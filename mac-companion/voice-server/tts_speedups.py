"""Speedups for Qwen3-TTS ICL streaming on M1-class GPUs (written against mlx-audio 0.6.4).

Measured on the M1 Pro (2026-11-03), each streamed sentence spent ~160 ms re-priming the
audio decoder with the voice reference, and each 80 ms audio frame spent ~24 ms in the
15-step code predictor, mostly building the lazy graph op by op. This module:

1. Runs the library's own 15-step code predictor loop (sampling included) through
   mx.compile, so each frame replays one traced graph instead of rebuilding it.
2. Caches the decoder streaming state after priming with a reference tail, per reference,
   and restores copies of it for later sentences.

Only the streaming ICL path is replaced; everything else calls the original method.
install() refuses to patch any other mlx-audio version.
"""
import logging
import time
import types
from importlib.metadata import version

import mlx.core as mx
import numpy as np

logger = logging.getLogger("xiaozhi-voice")

SUPPORTED_MLX_AUDIO = "0.6.4"
STATE_ATTRS = ("_buffer", "_overflow")
_KVCache = None
_GenerationResult = None
_format_duration = None


def _copy(a):
    return None if a is None else mx.array(a)


class _PrimerCache:
    """Decoder streaming state right after priming with a given reference tail."""

    def __init__(self, decoder):
        # load_model wraps the decoder in mx.compile; its state lives on the module behind the bound methods
        self.decoder = getattr(decoder.streaming_step, "__self__", decoder)
        self.modules = [m for _, m in self.decoder.named_modules() if hasattr(m, "reset_state")]
        self.saved = {}

    def prime(self, primer: mx.array) -> None:
        dec = self.decoder
        key = (primer.shape, np.asarray(primer).tobytes())
        snap = self.saved.get(key)
        dec.reset_streaming_state()
        if snap is None:
            mx.eval(dec.streaming_step(primer))
            kv = [(_copy(c.keys), _copy(c.values), c.offset) for c in dec._transformer_cache]
            bufs = [{a: _copy(getattr(m, a, None)) for a in STATE_ATTRS if hasattr(m, a)} for m in self.modules]
            mx.eval([x for k, v, _ in kv for x in (k, v) if x is not None],
                    [x for b in bufs for x in b.values() if x is not None])
            self.saved[key] = (kv, bufs)
            return
        kv, bufs = snap
        caches = []
        for k, v, off in kv:
            c = _KVCache()
            c.keys, c.values, c.offset = _copy(k), _copy(v), off
            caches.append(c)
        dec._transformer_cache = caches
        for m, b in zip(self.modules, bufs):
            for a, val in b.items():
                setattr(m, a, _copy(val))


def _make_frame_fn(model, temperature, top_k, top_p):
    """One compiled call per frame: code predictor steps 1..15 plus the next codec embedding."""
    talker = model.talker
    cp = talker.code_predictor
    groups = model.config.talker_config.num_code_groups
    embed0 = talker.get_input_embeddings()

    def frame(hidden_last, next_token):
        code_cache = cp.make_cache()
        code_tokens = [next_token]
        for code_idx in range(groups - 1):
            if code_idx == 0:
                code_input = mx.concatenate([hidden_last, embed0(next_token)], axis=1)
            else:
                code_input = cp.codec_embedding[code_idx - 1](code_tokens[-1])
            code_logits, code_cache, _ = cp(code_input, cache=code_cache, generation_step=code_idx)
            code_tokens.append(model._sample_token(code_logits, temperature=temperature, top_k=top_k, top_p=top_p))
        codec_embed = embed0(next_token)
        for i, code in enumerate(code_tokens[1:]):
            codec_embed = codec_embed + cp.codec_embedding[i](code)
        return mx.concatenate(code_tokens, axis=1), codec_embed

    state = [mx.random.state]
    return mx.compile(frame, inputs=state, outputs=state)


def _chunk_result(model, audio_chunk, new_tokens, chunk_start_time, final=False):
    chunk_elapsed = time.time() - chunk_start_time
    dur = audio_chunk.shape[0] / model.sample_rate
    return _GenerationResult(
        audio=audio_chunk,
        samples=audio_chunk.shape[0],
        sample_rate=model.sample_rate,
        segment_idx=0,
        token_count=new_tokens,
        audio_duration=_format_duration(dur),
        real_time_factor=dur / chunk_elapsed if chunk_elapsed > 0 else 0,
        prompt={"tokens": new_tokens, "tokens-per-sec": new_tokens / chunk_elapsed if chunk_elapsed > 0 else 0},
        audio_samples={"samples": audio_chunk.shape[0],
                       "samples-per-sec": audio_chunk.shape[0] / chunk_elapsed if chunk_elapsed > 0 else 0},
        processing_time_seconds=chunk_elapsed,
        peak_memory_usage=mx.get_peak_memory() / 1e9,
        is_streaming_chunk=True,
        **({"is_final_chunk": True} if final else {}),
    )


def install(model) -> bool:
    global _KVCache, _GenerationResult, _format_duration
    if version("mlx-audio") != SUPPORTED_MLX_AUDIO:
        logger.warning("tts_speedups: mlx-audio %s != %s, not patching", version("mlx-audio"), SUPPORTED_MLX_AUDIO)
        return False
    try:
        from mlx_audio.lm.models.cache import KVCache
        from mlx_audio.tts.models.base import GenerationResult
        from mlx_audio.tts.models.qwen3_tts.qwen3_tts import format_duration
    except ImportError as exc:
        logger.warning("tts_speedups: incompatible mlx-audio internals (%s), not patching", exc)
        return False
    _KVCache = KVCache
    _GenerationResult = GenerationResult
    _format_duration = format_duration
    original = model._generate_icl
    primers = _PrimerCache(model.speech_tokenizer.decoder)
    frame_fns = {}

    def _generate_icl(self, text, ref_audio, ref_text, language="auto", temperature=0.9, max_tokens=4096,
                      top_k=50, top_p=1.0, repetition_penalty=1.5, verbose=False, stream=False,
                      streaming_interval=2.0, streaming_context_size=25):
        if not stream or streaming_context_size <= 0:
            yield from original(text=text, ref_audio=ref_audio, ref_text=ref_text, language=language,
                                temperature=temperature, max_tokens=max_tokens, top_k=top_k, top_p=top_p,
                                repetition_penalty=repetition_penalty, verbose=verbose, stream=stream,
                                streaming_interval=streaming_interval, streaming_context_size=streaming_context_size)
            return

        input_embeds, trailing_text_hidden, tts_pad_embed, ref_codes = self._prepare_icl_generation_inputs(
            text=text, ref_audio=ref_audio, ref_text=ref_text, language=language)
        soft_max_tokens = getattr(self, "_current_soft_max_tokens", None)
        self._current_soft_max_tokens = None
        self._last_tts_stopped_by_eos = False
        self._last_tts_tokens = 0
        key = (float(temperature), int(top_k), float(top_p))
        frame_fn = frame_fns.get(key)
        if frame_fn is None:
            frame_fn = frame_fns[key] = _make_frame_fn(self, temperature, top_k, top_p)

        cache = self.talker.make_cache()
        config = self.config.talker_config
        eos_token_id = config.codec_eos_token_id
        suppress_tokens = [i for i in range(config.vocab_size - 1024, config.vocab_size) if i != eos_token_id]
        generated_codes, generated_token_ids = [], []
        trailing_idx = 0
        streaming_chunk_size = max(1, int(streaming_interval * 12.5))
        decoded_tokens = 0
        stopped_by_eos = False
        chunk_start_time = time.time()
        if ref_codes.shape[2] > 0:
            primers.prime(ref_codes[:, :, -streaming_context_size:])
        else:
            self.speech_tokenizer.decoder.reset_streaming_state()

        for step in range(max_tokens):
            logits, hidden = self.talker(input_embeds, cache=cache)
            if soft_max_tokens is not None and step >= soft_max_tokens:
                last_logits = logits[0, -1, :eos_token_id + 1]
                eos_gap = float((mx.max(last_logits) - last_logits[eos_token_id]).item())
                if eos_gap <= 3.0:
                    next_token = mx.array([[eos_token_id]], dtype=mx.int32)
                else:
                    cool_temp = max(0.35, float(temperature) * 0.5)
                    next_token = self._sample_token(
                        logits, temperature=cool_temp, top_k=min(top_k, 15), top_p=min(top_p, 0.80),
                        repetition_penalty=repetition_penalty,
                        generated_tokens=generated_token_ids or None, suppress_tokens=suppress_tokens)
            else:
                next_token = self._sample_token(
                    logits, temperature=temperature, top_k=top_k, top_p=top_p,
                    repetition_penalty=repetition_penalty,
                    generated_tokens=generated_token_ids or None, suppress_tokens=suppress_tokens)
            is_eos = next_token[0, 0] == eos_token_id
            all_codes, codec_embed = frame_fn(hidden[:, -1:, :], next_token)

            if trailing_idx < trailing_text_hidden.shape[1]:
                text_embed = trailing_text_hidden[:, trailing_idx:trailing_idx + 1, :]
                trailing_idx += 1
            else:
                text_embed = tts_pad_embed
            input_embeds = text_embed + codec_embed
            mx.eval(input_embeds, is_eos)
            if is_eos.item():
                stopped_by_eos = True
                break
            generated_token_ids.append(int(next_token[0, 0]))
            generated_codes.append(all_codes)

            if len(generated_codes) - decoded_tokens >= streaming_chunk_size and step + 1 < max_tokens:
                new_tokens = len(generated_codes) - decoded_tokens
                codes = mx.transpose(mx.stack(generated_codes[decoded_tokens:], axis=1), (0, 2, 1))
                audio_chunk = self.speech_tokenizer.decoder.streaming_step(codes).squeeze(1)[0]
                mx.eval(audio_chunk)
                decoded_tokens = len(generated_codes)
                yield _chunk_result(self, audio_chunk, new_tokens, chunk_start_time)
                chunk_start_time = time.time()

        self._last_tts_stopped_by_eos = stopped_by_eos
        self._last_tts_tokens = len(generated_codes)
        if not stopped_by_eos and len(generated_codes) >= max_tokens:
            logger.warning(
                "tts_speedups: hit max_tokens=%d without EOS (soft=%s) for text=%r",
                max_tokens, soft_max_tokens, text[:60],
            )

        if len(generated_codes) > decoded_tokens:
            new_tokens = len(generated_codes) - decoded_tokens
            codes = mx.transpose(mx.stack(generated_codes[decoded_tokens:], axis=1), (0, 2, 1))
            audio_chunk = self.speech_tokenizer.decoder.streaming_step(codes).squeeze(1)[0]
            if not stopped_by_eos and audio_chunk.shape[0] > 32:
                fade_n = min(600, int(audio_chunk.shape[0]) // 2)  # 25ms smooth fade-out on forced cap
                ramp = mx.linspace(1.0, 0.0, fade_n, dtype=audio_chunk.dtype)
                audio_chunk = mx.concatenate([audio_chunk[:-fade_n], audio_chunk[-fade_n:] * ramp], axis=0)
            mx.eval(audio_chunk)
            yield _chunk_result(self, audio_chunk, new_tokens, chunk_start_time, final=True)
        self.speech_tokenizer.decoder.reset_streaming_state()
        mx.clear_cache()

    model._generate_icl = types.MethodType(_generate_icl, model)
    model._speedup_primers = primers
    logger.info("tts_speedups: compiled code predictor + decoder primer cache enabled (mlx-audio %s)", SUPPORTED_MLX_AUDIO)
    return True


def warm_primer(
    model,
    ref_audio: mx.array,
    ref_text: str,
    language: str = "chinese",
    streaming_context_size: int = 25,
) -> None:
    """Prime and cache the decoder state for one voice reference ahead of its first sentence."""
    primers = getattr(model, "_speedup_primers", None)
    if primers is None:
        return
    ref_codes = model._prepare_icl_generation_inputs(
        text="你好。", ref_audio=ref_audio, ref_text=ref_text, language=language,
    )[3]
    if ref_codes.shape[2] > 0:
        primers.prime(ref_codes[:, :, -streaming_context_size:])
        primers.decoder.reset_streaming_state()
