' Runs the fast reply job with no window popping up (used by the "ClipMachine Replies" scheduled task)
Set fso = CreateObject("Scripting.FileSystemObject")
folder = fso.GetParentFolderName(WScript.ScriptFullName)
CreateObject("WScript.Shell").Run """" & folder & "\run-replies.bat""", 0, True
