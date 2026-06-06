' Silent admin launcher — only if Ctrl+Alt+R fails without elevation.
Set fso = CreateObject("Scripting.FileSystemObject")
appDir = fso.GetParentFolderName(WScript.ScriptFullName)
pythonw = appDir & "\venv\Scripts\pythonw.exe"
CreateObject("Shell.Application").ShellExecute pythonw, "run.py", appDir, "runas", 0
