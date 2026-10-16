# Standards for this repository

These rules apply to every task.

## Boundaries (these override everything else)

- Every change on the host happens inside this repository. Nothing is written
  outside this directory tree. An agent's own memory or global instruction
  file is the only possible exception, and only when I have agreed.
- Anything that would write outside this repo (installing packages into
  system/conda Python, global pip/conda caches, system tools, etc.) is done
  by me, not the agent. In those cases, stop and give me step-by-step
  commands to run myself, then continue once I confirm.

## Terminal

- Git Bash is the primary shell. Use it (POSIX syntax) for commands the
  agent runs, and write any commands given to me for Git Bash. Use
  PowerShell only if something can't be done in Git Bash.
- Anaconda isn't on PATH. Call it by full path, quoted because of the space:
  "$HOME/anaconda3/python.exe", "$HOME/anaconda3/Scripts/conda.exe".
- Calling a conda env's python.exe without activating it makes numpy/pandas
  imports die silently (exit 127). Put the env's DLL dirs on PATH first:
  E="$HOME/anaconda3/envs/qcodes"; export PATH="$E:$E/Library/mingw-w64/bin:$E/Library/usr/bin:$E/Library/bin:$E/Scripts:$PATH"
- Envs: `gds` (KLayout, for reading layouts), `qcodes` (pymeasure/pyvisa, for
  measurements).

## Reference manuals (read-only, outside this repo)

- S200 / Velox remote commands, one HTML page per command:
  "$HOME/OneDrive - Stanford/Cascade/S200_py_setup/FormFactor S200 Documentation/Manuals/Documentation/VeloxHTMLHelp/Content/VeloxRemoteInterface/COMMAND_NAME.htm"
  (station user guides are in ../Station User Guides/).
- B1500A FLEX programming guide and EasyEXPERT guides:
  "$HOME/OneDrive - Stanford/S200_B1500A_4200A_py_setup/References/b1500A_programming.pdf",
  b1500A_easy_expert_{1,2}.pdf.
- These are OneDrive files; the first read can be slow while they download.

## Instruments

- Never send GPIB/VISA commands to the real tools (S200, B1500A, ...)
  without my explicit approval for that specific run. Test with the fakes in
  measure/instruments/fakes.py (`python -m measure.tests.test_dryrun`).
