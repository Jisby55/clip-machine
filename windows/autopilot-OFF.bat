@echo off
schtasks /delete /f /tn "ClipMachine Autopilot" && echo AUTOPILOT OFF.
schtasks /delete /f /tn "ClipMachine Replies" && echo FAST REPLIES OFF.
pause
