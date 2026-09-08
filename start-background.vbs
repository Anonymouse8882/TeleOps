' TeleOps background launcher - starts the server with no visible window.
' Double-click to run. Use stop.bat to stop it.
' Put a shortcut in shell:startup (or Task Scheduler) for auto-start on boot.
'
' NOTE: keep this file pure ASCII. wscript.exe reads .vbs using the system ANSI
' codepage, so non-ASCII characters saved as UTF-8 turn into a syntax error and
' the script dies silently without launching anything.

Option Explicit

Dim fso, sh, root, py, cmd
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")

root = fso.GetParentFolderName(WScript.ScriptFullName)

py = root & "\.venv\Scripts\pythonw.exe"
If Not fso.FileExists(py) Then
    py = root & "\.venv\Scripts\python.exe"
End If

If Not fso.FileExists(py) Then
    MsgBox "Virtual environment not found." & vbCrLf & vbCrLf & _
           "Run these in the project folder first:" & vbCrLf & _
           "    python -m venv .venv" & vbCrLf & _
           "    .venv\Scripts\pip install -r requirements.txt", _
           vbCritical, "TeleOps"
    WScript.Quit 1
End If

sh.CurrentDirectory = root
cmd = """" & py & """ """ & root & "\run.py"""

' 0 = hidden window, False = do not wait for it to exit
sh.Run cmd, 0, False
