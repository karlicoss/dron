# Environment variables for macOS launchd jobs

`load_profile=True` lets dron jobs use the environment exported by `~/.profile`.
Without an explicit shell invocation, launchd does not read that file, so a scheduled job can have different `PATH`, Python and cache settings from the same command run in a terminal.
The goal is to maintain those settings in one place and make them available whenever a job starts.

The Linux setup that motivated this uses a systemd user environment generator to read the profile.
Systemd runs these generators before starting units, providing an ordering guarantee for the initial environment.
See the [systemd environment generator documentation](https://github.com/systemd/systemd/blob/main/man/systemd.environment-generator.xml).
We investigated ways to get similar behavior for macOS jobs and GUI applications in September 2026, on macOS **26.6.1 (25G76)**.

The closest general discussion we found is [Setting PATH and other environment variables](https://developer.apple.com/forums/thread/74371) on Apple's Developer Forums.
It describes several approaches below, including the problem with applications restored at login.
An Apple DTS engineer suggests a wrapper that loads the required environment and then executes the program.
It does not cover our later LoginHook and binary inspection findings, which are recorded below.

In dron, the job option adds `--load-profile` to [the launchd wrapper](../src/dron/launchd_wrapper.py).
The wrapper sources `~/.profile` in noninteractive `/bin/bash` before executing the job, and does the same separately for each failure notification command.
This initializes the child processes' environment; the Python wrapper itself keeps its inherited environment.
Profile changes take effect on the next invocation, with the cost of running the profile for each command.
This handles dron's processes rather than setting the environment for unrelated GUI applications.

The profile is sourced explicitly because a Bash login shell may choose `~/.bash_profile` instead, as described in the [Bash startup file documentation](https://www.gnu.org/s/bash/manual/html_node/Bash-Startup-Files.html).
The built-in Bash 3.2.57 successfully loaded the tested profile, including Homebrew, pyenv and Python/cache settings, starting from a minimal PATH.
That verifies this profile's compatibility; the feature still requires a profile that works with `/bin/bash` and does not require an interactive terminal.

The alternatives below distinguish documented mechanisms, observed test results and approaches we considered without implementing.

1. **A LaunchAgent that imports the environment with `launchctl setenv`.**

   This can update the environment inherited by subsequent launches in the affected launchd domain.
   However, `RunAtLoad` does not ensure the importer finishes before other LaunchAgents or restored GUI applications start.
   Those processes can inherit the earlier environment.
   Apple's [launchd startup documentation](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html) describes a model built around on-demand services rather than ordering ordinary startup jobs.

   [EnvPane](https://github.com/hschmidt/EnvPane#background) uses the same API as `launchctl setenv`, with an agent that imports settings after login and watches for changes.
   Its documentation explains why changes do not affect running applications.
   It is an existing implementation of an importer, but does not establish the startup ordering we wanted.

2. **A LoginHook using `launchctl asuser`.**

   This was tested during an actual logout/login, with a marker variable and both LaunchAgent and GUI application probes.
   The hook loaded the profile successfully and set the variables in `user/<uid>`, whose context was Background.
   Loginwindow then created the new `gui/<uid>` session after the hook returned.
   The probes launched in that GUI session did not receive the marker or the full imported environment.

   A manual test while the GUI session already existed had succeeded, so that test alone was misleading.
   Waiting inside the blocking hook for the new GUI domain would prevent loginwindow from reaching the step that created it.
   These are observations from the tested macOS version, not a claim that every historical LoginHook implementation behaves identically.
   The [launchctl manual](https://keith.github.io/xcode-man-pages/launchctl.1.html) documents the separate domains and the context switching performed by `asuser`.

3. **Persistent PATH configuration.**

   `launchctl config user path` persists a PATH setting and requires a reboot.
   The [manual](https://keith.github.io/xcode-man-pages/launchctl.1.html) explicitly limits this mechanism to PATH; it cannot initialize arbitrary variables such as `PYTHONNOUSERSITE` or `PYTHONPYCACHEPREFIX`.
   It therefore does not replace importing the profile.

4. **Values in each job or application plist.**

   Launchd's [`EnvironmentVariables`](https://keith.github.io/xcode-man-pages/launchd.plist.5.html) sets values for an individual job.
   [`LSEnvironment`](https://developer.apple.com/library/archive/documentation/General/Reference/InfoPlistKeyReference/Articles/LaunchServicesKeys.html#//apple_ref/doc/uid/20001431-106825) does something similar for applications started through Launch Services.
   These mechanisms work for explicit values, but do not source `~/.profile`.
   Using them would require maintaining or generating another representation of the settings, and updating it when the profile changes.

5. **Older environment configuration files.**

   Native support for `~/.MacOSX/environment.plist` was removed in OS X 10.8, as described in [EnvPane's background](https://github.com/hschmidt/EnvPane#background).
   The [launchctl manual](https://keith.github.io/xcode-man-pages/launchctl.1.html) also records that `/etc/launchd.conf` is no longer read and that `~/.launchd.conf` was documented but never implemented.
   Older instructions recommending these files are not applicable to the tested system.

6. **The `UserEnvironmentVariables` plist key.**

   [Older Apple launchd source](https://github.com/apple-oss-distributions/launchd/blob/main/src/core.c#L2795) contains a handler for this key, making it look promising as a way to set the environment for a whole session.
   We inspected the corresponding handler in the installed `/sbin/launchd` using disassembly.
   On macOS 26.6.1, it only logged that the key was not implemented and returned.
   This was a binary inspection result; we did not install a plist relying on the key.

7. **Delays or a separate job loader.**

   Sleeping inside an already launched wrapper does not refresh its environment after an importer calls `launchctl setenv`.
   The wrapper already has its own copy, which it passes to its children.

   Delaying the actual registration of jobs until an importer finishes could establish ordering for dron.
   We considered a single startup LaunchAgent that imports the environment and then bootstraps dron's job plists, replacing their individual autoload links.
   We did not implement it because sourcing the profile at command launch meets the requirement with less launchd management.
   Any such startup scheme would need to handle GUI login, including logout/login without a reboot; a delay measured only from system boot would not cover that case.
