"""NX-053 — Real Microsoft Windows UI Automation Native Backend.

Provides direct ctypes COM bindings to Windows UIAutomationCore.dll (IUIAutomation):
- CLSID_CUIAutomation {ff48dba4-60ef-4201-aa87-54103eef594e}
- IID_IUIAutomation {30cbe57d-d9d0-452a-ab13-7ac5ac4825ee}
- ElementFromHandle, FindFirst, FindAll
- ValuePattern (SetValue, CurrentValue)
- InvokePattern (Invoke)
- SetFocus
- Direct inspection and physical manipulation of real Windows UI Automation trees
"""

from __future__ import annotations

import ctypes
from ctypes import (
    HRESULT,
    POINTER,
    Structure,
    WINFUNCTYPE,
    byref,
    c_int,
    c_long,
    c_short,
    c_void_p,
    c_wchar_p,
    cast,
    wintypes,
)
from typing import Any, Sequence


UIA_BACKEND_NAME = "MICROSOFT_WINDOWS_UIA"
UIA_BACKEND_LIBRARY = "UIAutomationCore.dll"
UIA_NATIVE_API = "IUIAutomation"
MICROSOFT_UIA_BACKEND_PRESENT = True

# Pattern IDs
UIA_InvokePatternId = 10000
UIA_ValuePatternId = 10002
UIA_WindowPatternId = 10009
UIA_TransformPatternId = 10016

# Property IDs
UIA_ControlTypePropertyId = 30003
UIA_NamePropertyId = 30005
UIA_AutomationIdPropertyId = 30011
UIA_ClassNamePropertyId = 30004

# TreeScope
TreeScope_Element = 0x1
TreeScope_Children = 0x2
TreeScope_Descendants = 0x4
TreeScope_Subtree = 0x7

# Variant VT types
VT_BSTR = 8
VT_I4 = 3


class GUID(Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", wintypes.BYTE * 8),
    ]


def _make_guid(l: int, w1: int, w2: int, b1: int, b2: int, b3: int, b4: int, b5: int, b6: int, b7: int, b8: int) -> GUID:
    g = GUID()
    g.Data1 = l
    g.Data2 = w1
    g.Data3 = w2
    g.Data4 = (wintypes.BYTE * 8)(b1, b2, b3, b4, b5, b6, b7, b8)
    return g


CLSID_CUIAutomation = _make_guid(0xff48dba4, 0x60ef, 0x4201, 0xaa, 0x87, 0x54, 0x10, 0x3e, 0xef, 0x59, 0x4e)
IID_IUIAutomation = _make_guid(0x30cbe57d, 0xd9d0, 0x452a, 0xab, 0x13, 0x7a, 0xc5, 0xac, 0x48, 0x25, 0xee)


class VARIANT(Structure):
    class _U(ctypes.Union):
        _fields_ = [
            ("bstrVal", c_wchar_p),
            ("lVal", c_long),
            ("byref", c_void_p),
        ]
    _anonymous_ = ("_u",)
    _fields_ = [
        ("vt", c_short),
        ("wReserved1", wintypes.WORD),
        ("wReserved2", wintypes.WORD),
        ("wReserved3", wintypes.WORD),
        ("_u", _U),
    ]


class MicrosoftUIAutomationAdapter:
    """Production Microsoft UI Automation adapter calling native UIAutomationCore.dll."""

    def __init__(self) -> None:
        self.ole32 = ctypes.windll.ole32
        self.ole32.CoInitialize(None)
        self.p_uia = c_void_p()
        self.native_call_count: int = 0

        hr = self.ole32.CoCreateInstance(
            byref(CLSID_CUIAutomation),
            None,
            1,  # CLSCTX_INPROC_SERVER
            byref(IID_IUIAutomation),
            byref(self.p_uia),
        )
        if hr != 0 or not self.p_uia.value:
            raise RuntimeError(f"Failed to create CUIAutomation COM instance: hr={hex(hr & 0xffffffff)}")
        self.native_call_count += 1

    def _uia_vtbl(self, index: int) -> Any:
        vtbl = cast(self.p_uia, POINTER(POINTER(c_void_p)))[0]
        return vtbl[index]

    def _elem_vtbl(self, p_elem: c_void_p, index: int) -> Any:
        vtbl = cast(p_elem, POINTER(POINTER(c_void_p)))[0]
        return vtbl[index]

    def element_from_handle(self, hwnd: int) -> c_void_p:
        """IUIAutomation::ElementFromHandle (index 6)."""
        func_proto = WINFUNCTYPE(HRESULT, c_void_p, wintypes.HWND, POINTER(c_void_p))
        func = func_proto(self._uia_vtbl(6))
        p_elem = c_void_p()
        self.native_call_count += 1
        hr = func(self.p_uia, wintypes.HWND(hwnd), byref(p_elem))
        if hr != 0 or not p_elem.value:
            raise RuntimeError(f"ElementFromHandle failed for HWND {hwnd}: hr={hex(hr & 0xffffffff)}")
        return p_elem

    def get_element_name(self, p_elem: c_void_p) -> str:
        """IUIAutomationElement::get_CurrentName (index 23)."""
        func_proto = WINFUNCTYPE(HRESULT, c_void_p, POINTER(c_wchar_p))
        func = func_proto(self._elem_vtbl(p_elem, 23))
        name = c_wchar_p()
        self.native_call_count += 1
        hr = func(p_elem, byref(name))
        return name.value or "" if hr == 0 else ""

    def get_element_automation_id(self, p_elem: c_void_p) -> str:
        """IUIAutomationElement::get_CurrentAutomationId (index 29)."""
        func_proto = WINFUNCTYPE(HRESULT, c_void_p, POINTER(c_wchar_p))
        func = func_proto(self._elem_vtbl(p_elem, 29))
        aid = c_wchar_p()
        self.native_call_count += 1
        hr = func(p_elem, byref(aid))
        return aid.value or "" if hr == 0 else ""

    def get_element_class_name(self, p_elem: c_void_p) -> str:
        """IUIAutomationElement::get_CurrentClassName (index 30)."""
        func_proto = WINFUNCTYPE(HRESULT, c_void_p, POINTER(c_wchar_p))
        func = func_proto(self._elem_vtbl(p_elem, 30))
        cls = c_wchar_p()
        self.native_call_count += 1
        hr = func(p_elem, byref(cls))
        return cls.value or "" if hr == 0 else ""

    def get_element_control_type(self, p_elem: c_void_p) -> int:
        """IUIAutomationElement::get_CurrentControlType (index 21)."""
        func_proto = WINFUNCTYPE(HRESULT, c_void_p, POINTER(c_int))
        func = func_proto(self._elem_vtbl(p_elem, 21))
        ct = c_int()
        self.native_call_count += 1
        hr = func(p_elem, byref(ct))
        return ct.value if hr == 0 else 0

    def create_true_condition(self) -> c_void_p:
        """IUIAutomation::CreateTrueCondition (index 20)."""
        func_proto = WINFUNCTYPE(HRESULT, c_void_p, POINTER(c_void_p))
        func = func_proto(self._uia_vtbl(20))
        p_cond = c_void_p()
        self.native_call_count += 1
        func(self.p_uia, byref(p_cond))
        return p_cond

    def create_property_condition(self, property_id: int, value: str) -> c_void_p:
        """IUIAutomation::CreatePropertyCondition (index 22)."""
        func_proto = WINFUNCTYPE(HRESULT, c_void_p, c_int, VARIANT, POINTER(c_void_p))
        func = func_proto(self._uia_vtbl(22))
        var = VARIANT()
        var.vt = VT_BSTR
        var.bstrVal = value
        p_cond = c_void_p()
        self.native_call_count += 1
        hr = func(self.p_uia, c_int(property_id), var, byref(p_cond))
        return p_cond if hr == 0 else c_void_p()

    def find_first(self, p_root: c_void_p, scope: int, p_cond: c_void_p) -> c_void_p | None:
        """IUIAutomationElement::FindFirst (index 5)."""
        func_proto = WINFUNCTYPE(HRESULT, c_void_p, c_int, c_void_p, POINTER(c_void_p))
        func = func_proto(self._elem_vtbl(p_root, 5))
        p_found = c_void_p()
        self.native_call_count += 1
        hr = func(p_root, c_int(scope), p_cond, byref(p_found))
        if hr == 0 and p_found.value:
            return p_found
        return None

    def find_all(self, p_root: c_void_p, scope: int, p_cond: c_void_p) -> list[c_void_p]:
        """IUIAutomationElement::FindAll (index 6)."""
        func_proto = WINFUNCTYPE(HRESULT, c_void_p, c_int, c_void_p, POINTER(c_void_p))
        func = func_proto(self._elem_vtbl(p_root, 6))
        p_array = c_void_p()
        self.native_call_count += 1
        hr = func(p_root, c_int(scope), p_cond, byref(p_array))
        if hr != 0 or not p_array.value:
            return []

        # IUIAutomationElementArray::get_Length (index 3), GetElement (index 4)
        arr_vtbl = cast(p_array, POINTER(POINTER(c_void_p)))[0]
        get_Length = WINFUNCTYPE(HRESULT, c_void_p, POINTER(c_int))(arr_vtbl[3])
        get_Element = WINFUNCTYPE(HRESULT, c_void_p, c_int, POINTER(c_void_p))(arr_vtbl[4])

        length = c_int()
        self.native_call_count += 1
        get_Length(p_array, byref(length))

        elements: list[c_void_p] = []
        for i in range(length.value):
            elem = c_void_p()
            self.native_call_count += 1
            hr_el = get_Element(p_array, c_int(i), byref(elem))
            if hr_el == 0 and elem.value:
                elements.append(elem)
        return elements

    def set_focus(self, p_elem: c_void_p) -> bool:
        """IUIAutomationElement::SetFocus (index 3)."""
        func_proto = WINFUNCTYPE(HRESULT, c_void_p)
        func = func_proto(self._elem_vtbl(p_elem, 3))
        self.native_call_count += 1
        hr = func(p_elem)
        return bool(hr == 0)

    def get_pattern(self, p_elem: c_void_p, pattern_id: int) -> c_void_p | None:
        """IUIAutomationElement::GetCurrentPattern (index 16)."""
        func_proto = WINFUNCTYPE(HRESULT, c_void_p, c_int, POINTER(c_void_p))
        func = func_proto(self._elem_vtbl(p_elem, 16))
        p_pattern = c_void_p()
        self.native_call_count += 1
        hr = func(p_elem, c_int(pattern_id), byref(p_pattern))
        if hr == 0 and p_pattern.value:
            return p_pattern
        return None

    def set_value_pattern(self, p_elem: c_void_p, value: str) -> bool:
        """IUIAutomationValuePattern::SetValue (index 3)."""
        p_pat = self.get_pattern(p_elem, UIA_ValuePatternId)
        if not p_pat:
            return False
        pat_vtbl = cast(p_pat, POINTER(POINTER(c_void_p)))[0]
        set_Val = WINFUNCTYPE(HRESULT, c_void_p, c_wchar_p)(pat_vtbl[3])
        self.native_call_count += 1
        hr = set_Val(p_pat, value)
        return bool(hr == 0)

    def invoke_pattern(self, p_elem: c_void_p) -> bool:
        """IUIAutomationInvokePattern::Invoke (index 3)."""
        p_pat = self.get_pattern(p_elem, UIA_InvokePatternId)
        if not p_pat:
            return False
        pat_vtbl = cast(p_pat, POINTER(POINTER(c_void_p)))[0]
        do_invoke = WINFUNCTYPE(HRESULT, c_void_p)(pat_vtbl[3])
        self.native_call_count += 1
        hr = do_invoke(p_pat)
        return bool(hr == 0)

    def resize_window_native(self, hwnd: int, width: int, height: int) -> bool:
        """Resize window via Win32 SetWindowPos API."""
        user32 = ctypes.windll.user32
        self.native_call_count += 1
        # SWP_NOMOVE = 0x0002, SWP_NOZORDER = 0x0004
        ret = user32.SetWindowPos(hwnd, 0, 0, 0, width, height, 0x0002 | 0x0004)
        return bool(ret != 0)
