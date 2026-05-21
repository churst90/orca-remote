"""Unit tests for keymap.py (Windows VK -> X11 keysym).

Pure-function module. Run with:
    python3 -m pytest tests/test_keymap.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import keymap  # noqa: E402


class TestLetters:
    @pytest.mark.parametrize(
        "vk,keysym",
        [
            (0x41, 0x61),  # VK_A -> XK_a
            (0x4d, 0x6d),  # VK_M -> XK_m
            (0x5a, 0x7a),  # VK_Z -> XK_z
        ],
    )
    def test_letters_map_to_lowercase(self, vk: int, keysym: int) -> None:
        assert keymap.vk_to_keysym(vk) == keysym

    def test_letters_extended_flag_is_irrelevant(self) -> None:
        # Letters don't have an "extended" variant on Windows.
        assert keymap.vk_to_keysym(0x41, extended=True) == 0x61


class TestDigits:
    @pytest.mark.parametrize(
        "vk,keysym",
        [
            (0x30, 0x30),  # VK_0 -> XK_0
            (0x35, 0x35),  # VK_5 -> XK_5
            (0x39, 0x39),  # VK_9 -> XK_9
        ],
    )
    def test_main_row_digits(self, vk: int, keysym: int) -> None:
        assert keymap.vk_to_keysym(vk) == keysym

    @pytest.mark.parametrize(
        "vk,keysym",
        [
            (0x60, 0xffb0),  # VK_NUMPAD0 -> XK_KP_0
            (0x65, 0xffb5),  # VK_NUMPAD5 -> XK_KP_5
            (0x69, 0xffb9),  # VK_NUMPAD9 -> XK_KP_9
        ],
    )
    def test_keypad_digits(self, vk: int, keysym: int) -> None:
        assert keymap.vk_to_keysym(vk) == keysym


class TestNavCluster:
    """The nav cluster has the trickiest extended/non-extended split.

    Default (extended=False) gives the numpad keysym, which matches
    what Windows reports when NumLock is off and the user pressed
    a numpad arrow. Extended=True gives the main-row keysym.
    """

    @pytest.mark.parametrize(
        "vk,non_ext,ext",
        [
            (0x21, 0xff9a, 0xff55),  # VK_PRIOR (Page Up)
            (0x22, 0xff9b, 0xff56),  # VK_NEXT  (Page Down)
            (0x23, 0xff9c, 0xff57),  # VK_END
            (0x24, 0xff95, 0xff50),  # VK_HOME
            (0x25, 0xff96, 0xff51),  # VK_LEFT
            (0x26, 0xff97, 0xff52),  # VK_UP
            (0x27, 0xff98, 0xff53),  # VK_RIGHT
            (0x28, 0xff99, 0xff54),  # VK_DOWN
            (0x2D, 0xff9e, 0xff63),  # VK_INSERT
            (0x2E, 0xff9f, 0xffff),  # VK_DELETE
        ],
    )
    def test_nav_cluster(self, vk: int, non_ext: int, ext: int) -> None:
        assert keymap.vk_to_keysym(vk, extended=False) == non_ext
        assert keymap.vk_to_keysym(vk, extended=True) == ext


class TestModifiers:
    def test_generic_modifiers(self) -> None:
        assert keymap.vk_to_keysym(0x10) == 0xffe1  # Shift -> Shift_L
        assert keymap.vk_to_keysym(0x11) == 0xffe3  # Control -> Control_L
        assert keymap.vk_to_keysym(0x12) == 0xffe9  # Menu (Alt) -> Alt_L

    def test_left_right_modifiers(self) -> None:
        assert keymap.vk_to_keysym(0xA0) == 0xffe1  # LShift
        assert keymap.vk_to_keysym(0xA1) == 0xffe2  # RShift
        assert keymap.vk_to_keysym(0xA2) == 0xffe3  # LControl
        assert keymap.vk_to_keysym(0xA3) == 0xffe4  # RControl

    def test_extended_modifiers(self) -> None:
        # Windows reports right Ctrl/Alt as extended; we override to
        # the _R variants.
        assert keymap.vk_to_keysym(0x11, extended=True) == 0xffe4
        assert keymap.vk_to_keysym(0x12, extended=True) == 0xffea


class TestFunctionKeys:
    def test_f1_f12_range(self) -> None:
        assert keymap.vk_to_keysym(0x70) == 0xffbe  # F1
        assert keymap.vk_to_keysym(0x7b) == 0xffc9  # F12

    def test_f24_upper_bound(self) -> None:
        assert keymap.vk_to_keysym(0x87) == 0xffd5  # F24


class TestReturnAndKpEnter:
    def test_return(self) -> None:
        assert keymap.vk_to_keysym(0x0D) == 0xff0d

    def test_kp_enter_via_extended(self) -> None:
        # NVDA reports keypad Enter as extended VK_RETURN.
        assert keymap.vk_to_keysym(0x0D, extended=True) == 0xff8d


class TestBrowserAndMedia:
    def test_browser_keys(self) -> None:
        # XF86 keysyms live in the 0x1008xxxx range.
        assert keymap.vk_to_keysym(0xA6) == 0x1008ff26  # Back
        assert keymap.vk_to_keysym(0xAC) == 0x1008ff18  # HomePage

    def test_media_keys(self) -> None:
        assert keymap.vk_to_keysym(0xAD) == 0x1008ff12  # Mute
        assert keymap.vk_to_keysym(0xB3) == 0x1008ff14  # PlayPause


class TestIME:
    def test_ime_keys(self) -> None:
        assert keymap.vk_to_keysym(0x15) == 0xff31  # Hangul / Kana
        assert keymap.vk_to_keysym(0x19) == 0xff21  # Hanja / Kanji
        assert keymap.vk_to_keysym(0x1C) == 0xff26  # Convert -> Henkan_Mode
        assert keymap.vk_to_keysym(0x1D) == 0xff22  # NonConvert -> Muhenkan


class TestUnmappedFallthrough:
    def test_unknown_vk_returns_zero(self) -> None:
        assert keymap.vk_to_keysym(0xFE) == 0
        assert keymap.vk_to_keysym(0xFF) == 0
        assert keymap.vk_to_keysym(0x0E) == 0  # Hole in VK table

    def test_extended_for_unmapped_vk_still_returns_zero(self) -> None:
        # extended=True should not invent a mapping out of nowhere.
        assert keymap.vk_to_keysym(0xFE, extended=True) == 0


class TestTableCompleteness:
    def test_minimum_size(self) -> None:
        # Sanity floor so a future refactor doesn't accidentally drop
        # half the table. 26 letters + 10 digits + 10 KP digits + 24
        # F-keys + ~75 named/extended is comfortably >120.
        assert len(keymap._VK_TO_KEYSYM) >= 120
