' Force-reset stale lock and launch TinyReadAloud (use if "already running" but nothing in tray).
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("Wscript.Shell")
appDir = fso.GetParentFolderName(WScript.ScriptFullName)
lockFile = sh.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\TinyReadAloud\.instance.lock"
pythonw = appDir & "\venv\Scripts\pythonw.exe"

' Stop any real TinyReadAloud processes
Set wmi = GetObject("winmgmts:\\.\root\cimv2")
Set procs = wmi.ExecQuery("SELECT ProcessId, CommandLine FROM Win32_Process WHERE Name='pythonw.exe' OR Name='python.exe'")
For Each p In procs
    If InStr(p.CommandLine, "TinyReadAloud") > 0 And InStr(p.CommandLine, "run.py") > 0 Then
        On Error Resume Next
        sh.Run "taskkill /PID " & p.ProcessId & " /F", 0, True
        On Error GoTo 0
    End If
Next

If fso.FileExists(lockFile) Then fso.DeleteFile lockFile, True

sh.CurrentDirectory = appDir
sh.Run """" & pythonw & """ run.py", 0, False
