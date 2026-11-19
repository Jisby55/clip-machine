' Runs one autopilot cycle with no window popping up (used by the scheduled task)
Set fso = CreateObject("Scripting.FileSystemObject")
folder = fso.GetParentFolderName(WScript.ScriptFullName)
CreateObject("WScript.Shell").Run """" & folder & "\run-autopilot.bat""", 0, True
