Option Explicit
' InsightFlow dev launcher: starts the GUI with no console window.
' Uses pythonw.exe from PATH (the verified Python 3.10 install).
Dim sh, fso, base, env
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
base = fso.GetParentFolderName(WScript.ScriptFullName)
Set env = sh.Environment("PROCESS")
env("PYTHONPATH") = base & "\src"
env("PYTHONIOENCODING") = "utf-8"
env("INSIGHTFLOW_HOME") = base
sh.CurrentDirectory = base
sh.Run """pythonw.exe"" -m insightflow.gui", 0, False
