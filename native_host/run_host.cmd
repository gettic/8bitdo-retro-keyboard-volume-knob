@echo off
rem Starts the native-messaging host from source (Brave runs this; nothing may print to stdout).
python -I "%~dp0volume_knob_host.py" %*
