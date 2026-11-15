"""Build-time helper: Windows version resource and app manifest for JARVIS.exe.

Used by jarvis.spec and build_exe.py only; never imported by the app. The version comes from version.py
(APP_VERSION), parsed without importing it.
"""
import ast
import os

COMPANY = "Muhammad Talib Ibrahim"          # copyright holder per LICENSE
PRODUCT = "J.A.R.V.I.S."
DESCRIPTION = "J.A.R.V.I.S. - Touchless PC Control"
INTERNAL_NAME = "JARVIS"
EXE_NAME = "JARVIS.exe"
COPYRIGHT = "Copyright (c) 2026 Muhammad Talib Ibrahim"


def app_version(root):
    """APP_VERSION string from <root>/version.py, e.g. '2.6.0'."""
    with open(os.path.join(root, "version.py"), encoding="utf-8") as fp:
        tree = ast.parse(fp.read())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "APP_VERSION" for t in node.targets):
            return str(ast.literal_eval(node.value))
    raise RuntimeError("APP_VERSION not found in version.py")


def version_tuple(version):
    """'2.6.0' -> (2, 6, 0, 0); non-numeric suffixes ('2.7.0rc1') are dropped."""
    parts = []
    for p in version.split(".")[:4]:
        digits = "".join(ch for ch in p if ch.isdigit()) or "0"
        parts.append(int(digits))
    while len(parts) < 4:
        parts.append(0)
    return tuple(parts)


def make_version_info(root):
    """PyInstaller VSVersionInfo for JARVIS.exe."""
    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo, VarStruct, VSVersionInfo)

    ver = app_version(root)
    vt = version_tuple(ver)
    return VSVersionInfo(
        ffi=FixedFileInfo(filevers=vt, prodvers=vt, mask=0x3F, flags=0x0, OS=0x40004, fileType=0x1,
                          subtype=0x0, date=(0, 0)),
        kids=[
            StringFileInfo([StringTable("040904B0", [
                StringStruct("CompanyName", COMPANY),
                StringStruct("FileDescription", DESCRIPTION),
                StringStruct("FileVersion", ver),
                StringStruct("InternalName", INTERNAL_NAME),
                StringStruct("LegalCopyright", COPYRIGHT),
                StringStruct("OriginalFilename", EXE_NAME),
                StringStruct("ProductName", PRODUCT),
                StringStruct("ProductVersion", ver),
            ])]),
            VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
        ],
    )


# PyInstaller's default manifest plus Per-Monitor V2 DPI awareness. This declares the same awareness that
# main.py requests via SetProcessDpiAwarenessContext(-4), so the two never disagree (the API call then
# becomes a harmless no-op).
MANIFEST_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">
  <trustInfo xmlns="urn:schemas-microsoft-com:asm.v3">
    <security>
      <requestedPrivileges>
        <requestedExecutionLevel level="asInvoker" uiAccess="false"></requestedExecutionLevel>
      </requestedPrivileges>
    </security>
  </trustInfo>
  <compatibility xmlns="urn:schemas-microsoft-com:compatibility.v1">
    <application>
      <supportedOS Id="{e2011457-1546-43c5-a5fe-008deee3d3f0}"></supportedOS>
      <supportedOS Id="{35138b9a-5d96-4fbd-8e2d-a2440225f93a}"></supportedOS>
      <supportedOS Id="{4a2f28e3-53b9-4441-ba9c-d69d4a4a6e38}"></supportedOS>
      <supportedOS Id="{1f676c76-80e1-4239-95bb-83d0f6d0da78}"></supportedOS>
      <supportedOS Id="{8e0f7a12-bfb3-4fe8-b9a5-48fd50a15a9a}"></supportedOS>
    </application>
  </compatibility>
  <application xmlns="urn:schemas-microsoft-com:asm.v3">
    <windowsSettings>
      <longPathAware xmlns="http://schemas.microsoft.com/SMI/2016/WindowsSettings">true</longPathAware>
      <dpiAware xmlns="http://schemas.microsoft.com/SMI/2005/WindowsSettings">true/pm</dpiAware>
      <dpiAwareness xmlns="http://schemas.microsoft.com/SMI/2016/WindowsSettings">PerMonitorV2, PerMonitor</dpiAwareness>
    </windowsSettings>
  </application>
  <dependency>
    <dependentAssembly>
      <assemblyIdentity type="win32" name="Microsoft.Windows.Common-Controls" version="6.0.0.0" processorArchitecture="*" publicKeyToken="6595b64144ccf1df" language="*"></assemblyIdentity>
    </dependentAssembly>
  </dependency>
</assembly>
"""
