' OTHVM — start the desktop helper hidden in the interactive user session.
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
q = Chr(34)
sh.CurrentDirectory = dir
sh.Run q & dir & "\.venv\Scripts\python.exe" & q & " " & q & dir & "\main.py" & q & " --desktop", 0, False
