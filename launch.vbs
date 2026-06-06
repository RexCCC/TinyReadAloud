' Silent launcher — no black console flash. Default for Start menu.
Set sh = CreateObject("Wscript.Shell")
appDir = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
pythonw = appDir & "\venv\Scripts\pythonw.exe"
sh.CurrentDirectory = appDir
sh.Run """" & pythonw & """ run.py", 0, False
