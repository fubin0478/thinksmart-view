#!/usr/bin/env python3
import os
import signal
import sys
import struct
import threading
import time
import gi

gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
gi.require_version('GdkPixbuf', '2.0')
from gi.repository import Gtk, Gdk, GLib, GdkPixbuf


def _safe_quit():
    w = globals().get("win")
    if w is not None and hasattr(w, "decode_cond"):
        try:
            with w.decode_cond:
                w._closing = True
                w.decode_cond.notify_all()
        except Exception:
            pass
    if Gtk.main_level() > 0:
        Gtk.main_quit()
    return False


class IntercomWindow(Gtk.Window):
    def __init__(self):
        super().__init__(title="ThinkSmart Intercom")
        self.set_default_size(1280, 800)
        self.set_decorated(False)
        self.fullscreen()
        self.get_style_context().add_class("main-win")

        self.start_ts = time.monotonic()
        self.last_remote_ts = None
        self.last_local_ts = None
        self.last_elapsed = -1
        self.hung_up = False
        # Orientation state automatically synced from server (ftype 3)
        self.flip_h = True
        self.rot_deg = 0
        self.sensor_rot = 0
        self._applied_chrome_rot = None
        self.frame_lock = threading.Lock()
        self.decode_cond = threading.Condition(self.frame_lock)
        self.latest_f1 = None
        self.latest_f2 = None
        self.last_f1 = None
        self.last_f2 = None
        self.last_pb1 = None
        self.last_pb2 = None
        self.ready_f1 = None
        self.ready_f2 = None
        self._worker_pb1 = None
        self._worker_pb2 = None
        self.f1_fresh = False
        self.f2_fresh = False
        self.f1_seq = 0
        self.f2_seq = 0
        self.remote_off_pending = False
        self.flush_scheduled = False
        self._remote_shown = False
        self._closing = False

        overlay = Gtk.Overlay()
        self.add(overlay)

        # Center area: background + placeholder avatar when phone camera is off or connecting
        self.bg_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.bg_box.set_size_request(1280, 800)
        self.bg_box.set_valign(Gtk.Align.FILL)
        self.bg_box.set_halign(Gtk.Align.FILL)
        self.bg_box.get_style_context().add_class("bg-box")
        overlay.add(self.bg_box)

        self.avatar_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self.avatar_box.set_valign(Gtk.Align.CENTER)
        self.avatar_box.set_halign(Gtk.Align.CENTER)

        self.avatar_icon = Gtk.Label(label="📞")
        self.avatar_icon.get_style_context().add_class("avatar-circle")
        self.avatar_box.pack_start(self.avatar_icon, False, False, 0)

        self.avatar_title = Gtk.Label(label="手机远程可视对讲已接通")
        self.avatar_title.get_style_context().add_class("avatar-title")
        self.avatar_box.pack_start(self.avatar_title, False, False, 0)

        self.bg_box.pack_start(self.avatar_box, True, True, 0)

        # Fullscreen remote video image overlay
        self.remote_img = Gtk.Image()
        self.remote_img.set_valign(Gtk.Align.CENTER)
        self.remote_img.set_halign(Gtk.Align.CENTER)
        overlay.add_overlay(self.remote_img)
        overlay.set_overlay_pass_through(self.remote_img, True)

        # Top status pill
        self.top_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.top_box.get_style_context().add_class("status-pill")

        self.dot_lbl = Gtk.Label(label="●")
        self.dot_lbl.get_style_context().add_class("live-dot")
        self.top_box.pack_start(self.dot_lbl, False, False, 0)

        self.timer_lbl = Gtk.Label(label="可视对讲通话中  00:00")
        self.timer_lbl.get_style_context().add_class("status-text")
        self.top_box.pack_start(self.timer_lbl, False, False, 0)

        overlay.add_overlay(self.top_box)
        overlay.set_overlay_pass_through(self.top_box, True)

        # Bottom-right local PiP preview (hidden until local frame arrives)
        self.pip_frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.pip_frame.set_no_show_all(True)
        self.pip_frame.get_style_context().add_class("pip-frame")

        self.local_img = Gtk.Image()
        self.local_img.show()
        self.pip_frame.pack_start(self.local_img, False, False, 0)
        overlay.add_overlay(self.pip_frame)
        overlay.set_overlay_pass_through(self.pip_frame, True)

        # Bottom center Hangup button
        self.bottom_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=20)

        self.hangup_btn = Gtk.Button()
        self.hangup_lbl = Gtk.Label(label="  挂断通话  ")
        self.hangup_lbl.get_style_context().add_class("hangup-lbl")
        self.hangup_btn.add(self.hangup_lbl)
        self.hangup_btn.get_style_context().add_class("hangup-btn")
        self.hangup_btn.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.TOUCH_MASK)
        self.hangup_btn.connect("clicked", self.on_hangup)
        self.hangup_btn.connect("button-press-event", self._on_hangup_press)
        self.hangup_btn.connect("touch-event", self._on_hangup_touch)
        self.bottom_box.pack_start(self.hangup_btn, False, False, 0)
        overlay.add_overlay(self.bottom_box)

        css = b"""
        .main-win, .bg-box {
            background-color: #0b0f19;
        }
        .avatar-circle {
            background-color: #2563eb;
            color: #ffffff;
            font-size: 56px;
            border-radius: 999px;
            padding: 36px 44px;
            border: 4px solid rgba(59, 130, 246, 0.45);
        }
        .avatar-circle.rot-vert {
            padding: 44px 36px;
        }
        .avatar-title {
            color: #f8fafc;
            font-size: 28px;
            font-weight: bold;
        }
        .status-pill {
            background-color: rgba(15, 23, 42, 0.82);
            border-radius: 999px;
            padding: 10px 24px;
            border: 1px solid rgba(255, 255, 255, 0.18);
        }
        .status-pill.rot-vert {
            padding: 24px 10px;
        }
        .live-dot {
            color: #10b981;
            font-size: 20px;
        }
        .status-text {
            color: #ffffff;
            font-size: 20px;
            font-weight: bold;
        }
        .pip-frame {
            background-color: #000000;
            border: 2px solid rgba(255, 255, 255, 0.35);
            border-radius: 8px;
            padding: 2px;
        }
        .hangup-btn {
            background-image: none;
            background-color: #ef4444;
            color: #ffffff;
            border-radius: 32px;
            border: 2px solid rgba(255, 255, 255, 0.25);
            padding: 12px 24px;
        }
        .hangup-btn.rot-vert {
            padding: 24px 12px;
        }
        .hangup-lbl {
            color: #ffffff;
            font-size: 22px;
            font-weight: bold;
        }
        .hangup-btn:active {
            background-color: #dc2626;
        }
        """
        provider = Gtk.CssProvider()
        provider.load_from_data(css)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        self._apply_chrome_orientation(0)
        GLib.timeout_add(500, self.on_tick)
        threading.Thread(target=self._decode_worker_loop, daemon=True).start()

    @staticmethod
    def _reset_margins(w):
        w.set_margin_top(0)
        w.set_margin_bottom(0)
        w.set_margin_start(0)
        w.set_margin_end(0)

    def _apply_chrome_orientation(self, srot: int):
        srot = int(srot) % 360
        if self._applied_chrome_rot == srot:
            return
        self._applied_chrome_rot = srot
        # Gtk.Label.set_angle is counter-clockwise degrees; sensor_rot==90 is 90deg CW on Wayland surface.
        lbl_angle = (360 - srot) % 360
        vert = srot in (90, 270)
        for lbl in (self.avatar_icon, self.avatar_title, self.dot_lbl, self.timer_lbl, self.hangup_lbl):
            lbl.set_angle(lbl_angle)

        top_sc = self.top_box.get_style_context()
        btn_sc = self.hangup_btn.get_style_context()
        ava_sc = self.avatar_icon.get_style_context()
        if vert:
            top_sc.add_class("rot-vert")
            btn_sc.add_class("rot-vert")
            ava_sc.add_class("rot-vert")
            self.hangup_btn.set_size_request(68, 240)
        else:
            top_sc.remove_class("rot-vert")
            btn_sc.remove_class("rot-vert")
            ava_sc.remove_class("rot-vert")
            self.hangup_btn.set_size_request(240, 68)

        self._reset_margins(self.top_box)
        self._reset_margins(self.bottom_box)
        self._reset_margins(self.pip_frame)

        if srot == 0:
            self.avatar_box.set_orientation(Gtk.Orientation.VERTICAL)
            self.avatar_box.reorder_child(self.avatar_icon, 0)
            self.avatar_box.reorder_child(self.avatar_title, 1)

            self.top_box.set_orientation(Gtk.Orientation.HORIZONTAL)
            self.top_box.reorder_child(self.dot_lbl, 0)
            self.top_box.reorder_child(self.timer_lbl, 1)
            self.top_box.set_halign(Gtk.Align.CENTER)
            self.top_box.set_valign(Gtk.Align.START)
            self.top_box.set_margin_top(24)

            self.bottom_box.set_halign(Gtk.Align.CENTER)
            self.bottom_box.set_valign(Gtk.Align.END)
            self.bottom_box.set_margin_bottom(32)

            self.pip_frame.set_halign(Gtk.Align.END)
            self.pip_frame.set_valign(Gtk.Align.END)
            self.pip_frame.set_margin_end(28)
            self.pip_frame.set_margin_bottom(28)
        elif srot == 180:
            self.avatar_box.set_orientation(Gtk.Orientation.VERTICAL)
            self.avatar_box.reorder_child(self.avatar_title, 0)
            self.avatar_box.reorder_child(self.avatar_icon, 1)

            self.top_box.set_orientation(Gtk.Orientation.HORIZONTAL)
            self.top_box.reorder_child(self.timer_lbl, 0)
            self.top_box.reorder_child(self.dot_lbl, 1)
            self.top_box.set_halign(Gtk.Align.CENTER)
            self.top_box.set_valign(Gtk.Align.END)
            self.top_box.set_margin_bottom(24)

            self.bottom_box.set_halign(Gtk.Align.CENTER)
            self.bottom_box.set_valign(Gtk.Align.START)
            self.bottom_box.set_margin_top(32)

            self.pip_frame.set_halign(Gtk.Align.START)
            self.pip_frame.set_valign(Gtk.Align.START)
            self.pip_frame.set_margin_start(28)
            self.pip_frame.set_margin_top(28)
        elif srot == 90:
            self.avatar_box.set_orientation(Gtk.Orientation.HORIZONTAL)
            self.avatar_box.reorder_child(self.avatar_title, 0)
            self.avatar_box.reorder_child(self.avatar_icon, 1)

            self.top_box.set_orientation(Gtk.Orientation.VERTICAL)
            self.top_box.reorder_child(self.dot_lbl, 0)
            self.top_box.reorder_child(self.timer_lbl, 1)
            self.top_box.set_halign(Gtk.Align.END)
            self.top_box.set_valign(Gtk.Align.CENTER)
            self.top_box.set_margin_end(24)

            self.bottom_box.set_halign(Gtk.Align.START)
            self.bottom_box.set_valign(Gtk.Align.CENTER)
            self.bottom_box.set_margin_start(32)

            self.pip_frame.set_halign(Gtk.Align.START)
            self.pip_frame.set_valign(Gtk.Align.END)
            self.pip_frame.set_margin_start(116)
            self.pip_frame.set_margin_bottom(24)
        else:  # srot == 270
            self.avatar_box.set_orientation(Gtk.Orientation.HORIZONTAL)
            self.avatar_box.reorder_child(self.avatar_icon, 0)
            self.avatar_box.reorder_child(self.avatar_title, 1)

            self.top_box.set_orientation(Gtk.Orientation.VERTICAL)
            self.top_box.reorder_child(self.timer_lbl, 0)
            self.top_box.reorder_child(self.dot_lbl, 1)
            self.top_box.set_halign(Gtk.Align.START)
            self.top_box.set_valign(Gtk.Align.CENTER)
            self.top_box.set_margin_start(24)

            self.bottom_box.set_halign(Gtk.Align.END)
            self.bottom_box.set_valign(Gtk.Align.CENTER)
            self.bottom_box.set_margin_end(32)

            self.pip_frame.set_halign(Gtk.Align.END)
            self.pip_frame.set_valign(Gtk.Align.START)
            self.pip_frame.set_margin_end(116)
            self.pip_frame.set_margin_top(24)

    def _on_hangup_press(self, _widget, _event):
        self.on_hangup(_widget)
        return True

    def _on_hangup_touch(self, _widget, event):
        if event.touch.type == Gdk.EventType.TOUCH_BEGIN:
            self.on_hangup(_widget)
            return True
        return False

    def on_hangup(self, _widget):
        if self.hung_up:
            return
        self.hung_up = True
        try:
            sys.stdout.write("HANGUP\n")
            sys.stdout.flush()
        except Exception:
            pass
        _safe_quit()

    def on_tick(self):
        now = time.monotonic()
        elapsed = int(now - self.start_ts)
        if elapsed != self.last_elapsed:
            self.last_elapsed = elapsed
            hh, rem = divmod(max(0, elapsed), 3600)
            mm, ss = divmod(rem, 60)
            ts = f"{hh}:{mm:02d}:{ss:02d}" if hh else f"{mm:02d}:{ss:02d}"
            self.timer_lbl.set_text(f"可视对讲通话中  {ts}")
        timed_out_remote = False
        timed_out_local = False
        with self.decode_cond:
            if not self.f1_fresh and self.last_remote_ts is not None and (now - self.last_remote_ts) > 3.5:
                self.last_remote_ts = None
                self.last_f1 = None
                self.latest_f1 = None
                self.ready_f1 = None
                self.f1_fresh = False
                timed_out_remote = True
            if not self.f2_fresh and self.last_local_ts is not None and (now - self.last_local_ts) > 2.5:
                self.last_local_ts = None
                self.last_f2 = None
                self.latest_f2 = None
                self.ready_f2 = None
                self.f2_fresh = False
                timed_out_local = True
            has_remote = self.last_remote_ts is not None
            f1_busy = (self.latest_f1 is not None or self.ready_f1 is not None)
            if timed_out_remote or timed_out_local:
                self.decode_cond.notify_all()
        if timed_out_remote or (self._remote_shown and not has_remote and not f1_busy):
            self._remote_shown = False
            self.last_pb1 = None
            self.remote_img.clear()
            self.avatar_box.show()
        if timed_out_local:
            self.last_pb2 = None
            self.local_img.clear()
            self.pip_frame.hide()
        return True

    @staticmethod
    def rotate_cw(pb, deg):
        deg = deg % 360
        if deg == 90:
            return pb.rotate_simple(GdkPixbuf.PixbufRotation.CLOCKWISE)
        elif deg == 180:
            return pb.rotate_simple(GdkPixbuf.PixbufRotation.UPSIDEDOWN)
        elif deg == 270:
            return pb.rotate_simple(GdkPixbuf.PixbufRotation.COUNTERCLOCKWISE)
        return pb

    def apply_orientation(self, pb, is_local_pip=False, rot_deg=None, flip_h=None, sensor_rot=None):
        rot_deg = self.rot_deg if rot_deg is None else rot_deg
        flip_h = self.flip_h if flip_h is None else flip_h
        sensor_rot = self.sensor_rot if sensor_rot is None else sensor_rot
        if is_local_pip:
            if flip_h:
                pb = pb.flip(True)
                pb = self.rotate_cw(pb, (sensor_rot - rot_deg) % 360)
            else:
                pb = self.rotate_cw(pb, (rot_deg + sensor_rot) % 360)
        else:
            pb = self.rotate_cw(pb, sensor_rot)
        return pb

    def decode_jpg(self, data, target_box=None):
        loader = GdkPixbuf.PixbufLoader.new_with_type('jpeg')
        if target_box:
            tw, th = target_box
            def _on_size(ldr, w, h):
                if w > 0 and h > 0 and (w > tw or h > th):
                    if w >= h:
                        bw, bh = tw, th
                    else:
                        bw, bh = th, tw
                    sc = min(bw / float(w), bh / float(h))
                    if sc < 1.0:
                        ldr.set_size(max(1, int(w * sc)), max(1, int(h * sc)))
            loader.connect("size-prepared", _on_size)
        ok_w = False
        ok_c = False
        try:
            ok_w = bool(loader.write(data))
        except Exception:
            ok_w = False
        finally:
            try:
                ok_c = bool(loader.close())
            except Exception:
                ok_c = False
        if not (ok_w and ok_c):
            return None
        return loader.get_pixbuf()

    def _decode_worker_loop(self):
        while True:
            with self.decode_cond:
                while (
                    not self._closing
                    and self.latest_f1 is None
                    and self.latest_f2 is None
                    and not (self._worker_pb1 is not None and self.last_remote_ts is None and not self.f1_fresh)
                    and not (self._worker_pb2 is not None and self.last_local_ts is None and not self.f2_fresh)
                ):
                    self.decode_cond.wait()
                if self._closing:
                    return
                f1 = self.latest_f1 if (self.f1_fresh or self.last_remote_ts is not None) else None
                f2 = self.latest_f2
                clear_pb1 = (f1 is None and self.last_remote_ts is None and not self.f1_fresh)
                clear_pb2 = (f2 is None and self.last_local_ts is None and not self.f2_fresh)
                if clear_pb1:
                    self._worker_pb1 = None
                if clear_pb2:
                    self._worker_pb2 = None
                f1_fresh = self.f1_fresh
                f2_fresh = self.f2_fresh
                f1_seq = self.f1_seq
                f2_seq = self.f2_seq
                self.latest_f1 = None
                self.latest_f2 = None
                self.f1_fresh = False
                self.f2_fresh = False
                rot_deg = self.rot_deg
                flip_h = self.flip_h
                sensor_rot = self.sensor_rot

            sched = False
            if f1 is not None:
                f1_decoded = False
                try:
                    pb_entry = self._worker_pb1
                    pb = pb_entry[1] if (pb_entry and pb_entry[0] is f1) else self.decode_jpg(f1, target_box=(1280, 800))
                    if pb and pb.get_width() > 0 and pb.get_height() > 0:
                        self._worker_pb1 = (f1, pb)
                        oriented = self.apply_orientation(pb, is_local_pip=False, rot_deg=rot_deg, flip_h=flip_h, sensor_rot=sensor_rot)
                        pw, ph = oriented.get_width(), oriented.get_height()
                        if pw > 0 and ph > 0:
                            scale = min(1280.0 / pw, 800.0 / ph)
                            sw = max(1, int(pw * scale))
                            sh = max(1, int(ph * scale))
                            scaled = oriented if (sw == pw and sh == ph) else oriented.scale_simple(sw, sh, GdkPixbuf.InterpType.BILINEAR)
                            requeue_f1 = False
                            with self.frame_lock:
                                if (rot_deg, flip_h, sensor_rot) != (self.rot_deg, self.flip_h, self.sensor_rot):
                                    requeue_f1 = (self.f1_seq == f1_seq and not self.remote_off_pending)
                                else:
                                    if f1_fresh and not self.remote_off_pending and self.f1_seq == f1_seq:
                                        self.last_remote_ts = time.monotonic()
                                    active_remote = (
                                        not self.remote_off_pending
                                        and self.f1_seq == f1_seq
                                        and self.last_remote_ts is not None
                                    )
                                    if active_remote:
                                        self.last_f1 = f1
                                        self.ready_f1 = (f1_seq, scaled)
                                        sched = True
                                        f1_decoded = True
                            if requeue_f1:
                                f1_decoded = True
                                with self.decode_cond:
                                    if self.f1_seq == f1_seq and not self.remote_off_pending and self.latest_f1 is None:
                                        self.latest_f1 = f1
                                        self.f1_fresh = self.f1_fresh or f1_fresh
                                        self.decode_cond.notify_all()
                except Exception:
                    pass
                if not f1_decoded:
                    with self.frame_lock:
                        if self.last_remote_ts is None:
                            sched = True

            if f2 is not None:
                try:
                    pb2_entry = self._worker_pb2
                    if pb2_entry and pb2_entry[0] is f2:
                        scaled_pip = pb2_entry[1]
                    else:
                        pb = self.decode_jpg(f2, target_box=(256, 192))
                        scaled_pip = None
                        if pb:
                            pw, ph = pb.get_width(), pb.get_height()
                            if pw > 0 and ph > 0:
                                tw, th = (256, 192) if pw >= ph else (192, 256)
                                scaled_pip = pb if (pw == tw and ph == th) else pb.scale_simple(tw, th, GdkPixbuf.InterpType.BILINEAR)
                                self._worker_pb2 = (f2, scaled_pip)
                    if scaled_pip:
                        oriented_pip = self.apply_orientation(scaled_pip, is_local_pip=True, rot_deg=rot_deg, flip_h=flip_h, sensor_rot=sensor_rot)
                        requeue_f2 = False
                        with self.frame_lock:
                            if (rot_deg, flip_h, sensor_rot) != (self.rot_deg, self.flip_h, self.sensor_rot):
                                requeue_f2 = (self.f2_seq == f2_seq)
                            else:
                                if f2_fresh and self.f2_seq == f2_seq:
                                    self.last_local_ts = time.monotonic()
                                if self.f2_seq == f2_seq and self.last_local_ts is not None:
                                    self.last_f2 = f2
                                    self.ready_f2 = (f2_seq, oriented_pip)
                                    sched = True
                        if requeue_f2:
                            with self.decode_cond:
                                if self.f2_seq == f2_seq and self.latest_f2 is None:
                                    self.latest_f2 = f2
                                    self.f2_fresh = self.f2_fresh or f2_fresh
                                    self.decode_cond.notify_all()
                except Exception:
                    pass

            if sched:
                with self.frame_lock:
                    if not self.flush_scheduled:
                        self.flush_scheduled = True
                        GLib.idle_add(self.flush_frames)

    def enqueue_frame(self, ftype, data):
        with self.decode_cond:
            need_ui = False
            need_decode = False
            if ftype == 3:
                if len(data) < 2:
                    return
                new_rot = (int(data[0]) & 3) * 90
                new_flip = bool(data[1])
                new_srot = (int(data[2]) & 3) * 90 if len(data) >= 3 else self.sensor_rot
                if (new_rot, new_flip, new_srot) == (self.rot_deg, self.flip_h, self.sensor_rot):
                    return
                self.rot_deg, self.flip_h, self.sensor_rot = new_rot, new_flip, new_srot
                if self.latest_f1 is None and self.last_f1 is not None and self.last_remote_ts is not None:
                    self.latest_f1 = self.last_f1
                    need_decode = True
                elif self.latest_f1 is not None:
                    need_decode = True
                if self.latest_f2 is None and self.last_f2 is not None and self.last_local_ts is not None:
                    self.latest_f2 = self.last_f2
                    need_decode = True
                elif self.latest_f2 is not None:
                    need_decode = True
                need_ui = True
            elif ftype == 4:
                self.f1_seq += 1
                self.last_remote_ts = None
                self.last_f1 = None
                self.latest_f1 = None
                self.ready_f1 = None
                self.f1_fresh = False
                self.remote_off_pending = True
                need_decode = True
                need_ui = True
            elif ftype == 1:
                self.f1_seq += 1
                self.latest_f1 = data
                self.f1_fresh = True
                self.remote_off_pending = False
                need_decode = True
            elif ftype == 2:
                self.f2_seq += 1
                self.latest_f2 = data
                self.f2_fresh = True
                need_decode = True
            else:
                return
            if need_decode:
                self.decode_cond.notify_all()
            if need_ui and not self.flush_scheduled:
                self.flush_scheduled = True
                GLib.idle_add(self.flush_frames)

    def flush_frames(self):
        with self.frame_lock:
            remote_off = self.remote_off_pending
            self.remote_off_pending = False
            r1 = self.ready_f1
            r2 = self.ready_f2
            self.ready_f1 = None
            self.ready_f2 = None
            cur_f1_seq = self.f1_seq
            cur_f2_seq = self.f2_seq
            has_remote = self.last_remote_ts is not None
            has_local = self.last_local_ts is not None
            f1_pending = self.latest_f1 is not None
            self.flush_scheduled = False
            sensor_rot = self.sensor_rot

        self._apply_chrome_orientation(sensor_rot)
        valid_r1 = r1 is not None and r1[0] == cur_f1_seq and has_remote
        if valid_r1:
            self._remote_shown = True
            self.remote_img.set_from_pixbuf(r1[1])
            if self.avatar_box.get_visible():
                self.avatar_box.hide()
        elif remote_off or (self._remote_shown and not has_remote and not f1_pending):
            self._remote_shown = False
            self.last_pb1 = None
            self.remote_img.clear()
            self.avatar_box.show()

        if r2 is not None and r2[0] == cur_f2_seq and has_local:
            self.local_img.set_from_pixbuf(r2[1])
            if not self.pip_frame.get_visible():
                self.pip_frame.show()
        return False


def _read_exact(fd, n):
    if n == 0:
        return b""
    chunk = fd.read(n)
    if not chunk:
        return None
    if len(chunk) == n:
        return chunk
    buf = bytearray(chunk)
    while len(buf) < n:
        more = fd.read(n - len(buf))
        if not more:
            return None
        buf.extend(more)
    return bytes(buf)


def stdin_reader(win):
    fd = sys.stdin.buffer
    try:
        while True:
            hdr = _read_exact(fd, 5)
            if not hdr:
                break
            ftype = hdr[0]
            flen = struct.unpack("<I", hdr[1:5])[0]
            if flen > 5_000_000:
                break
            data = _read_exact(fd, flen)
            if data is None:
                break
            win.enqueue_frame(ftype, data)
    except Exception:
        pass
    GLib.idle_add(_safe_quit)


if __name__ == "__main__":
    win = IntercomWindow()
    win.connect("destroy", lambda *_: _safe_quit())
    win.show_all()
    for _sig in (signal.SIGTERM, signal.SIGINT):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, _sig, lambda *_: _safe_quit())
    t = threading.Thread(target=stdin_reader, args=(win,), daemon=True)
    t.start()
    Gtk.main()
    sys.stdout.flush()
    os._exit(0)
