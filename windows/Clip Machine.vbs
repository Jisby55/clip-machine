' Opens the Clip Machine app with no console window
Set fso = CreateObject("Scripting.FileSystemObject")
folder = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))  ' the project folder (one up from windows\)
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = folder
sh.Run "pythonw """ & folder & "\app.py""", 0, False
