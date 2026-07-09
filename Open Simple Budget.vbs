Set WshShell = CreateObject("WScript.Shell")
pythonw = "C:\Users\fmueg\Documents\py314\pythonw.exe"
script = "C:\Users\fmueg\Documents\simple-budget\launch.py"
WshShell.Run """" & pythonw & """ """ & script & """", 0, False
