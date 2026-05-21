"""Windows Virtual-Key code -> X11 keysym translation table.

NVDA Remote v2.x `key` messages carry the originating machine's
Windows virtual key code (`vk_code`). To replay the keystroke on a
Linux slave we have to map that into an X11 keysym that
Atspi.generate_keyboard_event understands.

The table below covers the keys real users actually press:
letters, digits, arrows, function keys, modifiers, common
punctuation, and the dead keys that interact with Orca. Anything
unmapped returns 0; the caller logs and drops it rather than
guessing.

Keysym values are the standard X.org constants from
/usr/include/X11/keysymdef.h. We hardcode rather than depending on
python-xlib so the extension stays stdlib-only.
"""

from __future__ import annotations


# Letters: Windows VK_A..VK_Z (0x41..0x5A) map to lowercase X keysyms
# (a..z, 0x61..0x7a). NVDA Remote sends VK_SHIFT separately when the
# user is holding shift, so we never emit the uppercase keysym here.
_LETTERS: dict[int, int] = {
    0x41 + i: 0x61 + i for i in range(26)
}

# Digits: VK_0..VK_9 (0x30..0x39) -> X keysyms 0x30..0x39 (same).
_DIGITS: dict[int, int] = {
    0x30 + i: 0x30 + i for i in range(10)
}

# Numeric keypad digits: VK_NUMPAD0..VK_NUMPAD9 (0x60..0x69) ->
# XK_KP_0..XK_KP_9 (0xffb0..0xffb9).
_KEYPAD_DIGITS: dict[int, int] = {
    0x60 + i: 0xffb0 + i for i in range(10)
}

# Function keys: VK_F1..VK_F24 (0x70..0x87) -> XK_F1..XK_F24
# (0xffbe..0xffd5).
_FUNCTION_KEYS: dict[int, int] = {
    0x70 + i: 0xffbe + i for i in range(24)
}

# Named keys -- the catalogue of everything that isn't a letter,
# digit, or F-key. Keysyms taken from keysymdef.h.
_NAMED: dict[int, int] = {
    0x08: 0xff08,  # VK_BACK        -> XK_BackSpace
    0x09: 0xff09,  # VK_TAB         -> XK_Tab
    0x0C: 0xff0b,  # VK_CLEAR       -> XK_Clear
    0x0D: 0xff0d,  # VK_RETURN      -> XK_Return
    0x10: 0xffe1,  # VK_SHIFT       -> XK_Shift_L
    0x11: 0xffe3,  # VK_CONTROL     -> XK_Control_L
    0x12: 0xffe9,  # VK_MENU (Alt)  -> XK_Alt_L
    0x13: 0xff13,  # VK_PAUSE       -> XK_Pause
    0x14: 0xffe5,  # VK_CAPITAL     -> XK_Caps_Lock
    0x1B: 0xff1b,  # VK_ESCAPE      -> XK_Escape
    0x20: 0x0020,  # VK_SPACE       -> XK_space
    # Nav-cluster keys default to the *numpad* keysym (extended=False
    # is what Windows reports when NumLock is off and the user pressed
    # the numpad nav keys -- e.g. numpad-0 for Insert). The main-row
    # variants land in _EXTENDED_OVERRIDES below. This matters because
    # Orca's desktop layout binds flat review, screen-reader commands,
    # etc. to the XK_KP_* keysyms; mapping a remote numpad press to
    # the main-row keysym would silently miss every Orca binding.
    0x21: 0xff9a,  # VK_PRIOR       -> XK_KP_Page_Up
    0x22: 0xff9b,  # VK_NEXT        -> XK_KP_Page_Down
    0x23: 0xff9c,  # VK_END         -> XK_KP_End
    0x24: 0xff95,  # VK_HOME        -> XK_KP_Home
    0x25: 0xff96,  # VK_LEFT        -> XK_KP_Left
    0x26: 0xff97,  # VK_UP          -> XK_KP_Up
    0x27: 0xff98,  # VK_RIGHT       -> XK_KP_Right
    0x28: 0xff99,  # VK_DOWN        -> XK_KP_Down
    0x2C: 0xff61,  # VK_SNAPSHOT    -> XK_Print
    0x2D: 0xff9e,  # VK_INSERT      -> XK_KP_Insert
    0x2E: 0xff9f,  # VK_DELETE      -> XK_KP_Delete
    0x5B: 0xffeb,  # VK_LWIN        -> XK_Super_L
    0x5C: 0xffec,  # VK_RWIN        -> XK_Super_R
    0x5D: 0xff67,  # VK_APPS        -> XK_Menu
    0x6A: 0xffaa,  # VK_MULTIPLY    -> XK_KP_Multiply
    0x6B: 0xffab,  # VK_ADD         -> XK_KP_Add
    0x6C: 0xffac,  # VK_SEPARATOR   -> XK_KP_Separator
    0x6D: 0xffad,  # VK_SUBTRACT    -> XK_KP_Subtract
    0x6E: 0xffae,  # VK_DECIMAL     -> XK_KP_Decimal
    0x6F: 0xffaf,  # VK_DIVIDE      -> XK_KP_Divide
    0x90: 0xff7f,  # VK_NUMLOCK     -> XK_Num_Lock
    0x91: 0xff14,  # VK_SCROLL      -> XK_Scroll_Lock
    0xA0: 0xffe1,  # VK_LSHIFT      -> XK_Shift_L
    0xA1: 0xffe2,  # VK_RSHIFT      -> XK_Shift_R
    0xA2: 0xffe3,  # VK_LCONTROL    -> XK_Control_L
    0xA3: 0xffe4,  # VK_RCONTROL    -> XK_Control_R
    0xA4: 0xffe9,  # VK_LMENU       -> XK_Alt_L
    0xA5: 0xffea,  # VK_RMENU       -> XK_Alt_R
    0xBA: 0x003b,  # VK_OEM_1       -> XK_semicolon
    0xBB: 0x003d,  # VK_OEM_PLUS    -> XK_equal
    0xBC: 0x002c,  # VK_OEM_COMMA   -> XK_comma
    0xBD: 0x002d,  # VK_OEM_MINUS   -> XK_minus
    0xBE: 0x002e,  # VK_OEM_PERIOD  -> XK_period
    0xBF: 0x002f,  # VK_OEM_2       -> XK_slash
    0xC0: 0x0060,  # VK_OEM_3       -> XK_grave
    0xDB: 0x005b,  # VK_OEM_4       -> XK_bracketleft
    0xDC: 0x005c,  # VK_OEM_5       -> XK_backslash
    0xDD: 0x005d,  # VK_OEM_6       -> XK_bracketright
    0xDE: 0x0027,  # VK_OEM_7       -> XK_apostrophe
}


_VK_TO_KEYSYM: dict[int, int] = {
    **_LETTERS,
    **_DIGITS,
    **_KEYPAD_DIGITS,
    **_FUNCTION_KEYS,
    **_NAMED,
}


# Windows reports `extended=True` for keys on the "enhanced keyboard"
# extended set: right Ctrl/Alt, numpad Enter and slash, and the
# main-row nav cluster to the left of the numeric keypad (Insert,
# Delete, Home, End, Page Up, Page Down, and arrow keys). For the
# nav keys this is the *inverse* of how the _NAMED defaults are set
# up -- the defaults give the numpad keysym, and an extended press
# overrides it to the main-row keysym.
_EXTENDED_OVERRIDES: dict[int, int] = {
    0x0D: 0xff8d,  # extended VK_RETURN  -> XK_KP_Enter
    0x11: 0xffe4,  # extended VK_CONTROL -> XK_Control_R
    0x12: 0xffea,  # extended VK_MENU    -> XK_Alt_R
    0x21: 0xff55,  # extended VK_PRIOR   -> XK_Page_Up
    0x22: 0xff56,  # extended VK_NEXT    -> XK_Page_Down
    0x23: 0xff57,  # extended VK_END     -> XK_End
    0x24: 0xff50,  # extended VK_HOME    -> XK_Home
    0x25: 0xff51,  # extended VK_LEFT    -> XK_Left
    0x26: 0xff52,  # extended VK_UP      -> XK_Up
    0x27: 0xff53,  # extended VK_RIGHT   -> XK_Right
    0x28: 0xff54,  # extended VK_DOWN    -> XK_Down
    0x2D: 0xff63,  # extended VK_INSERT  -> XK_Insert
    0x2E: 0xffff,  # extended VK_DELETE  -> XK_Delete
}


def vk_to_keysym(vk_code: int, extended: bool = False) -> int:
    """Translate a Windows VK code (+ extended flag) to an X11 keysym.

    Returns 0 if the VK code is not in the table; callers should
    log and skip the event rather than synthesize a bogus keysym.
    """

    if extended and vk_code in _EXTENDED_OVERRIDES:
        return _EXTENDED_OVERRIDES[vk_code]
    return _VK_TO_KEYSYM.get(vk_code, 0)
