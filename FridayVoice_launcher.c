// Minimal Mach-O launcher for the FridayVoice .app bundle.
//
// macOS TCC tracks microphone permission against the "responsible code" of
// the requesting process. A #!/bin/bash shell-script launcher fails to
// attach the bundle as the responsible code, so TCC matches on the bare
// python3.12 codesign identity and silently inherits whatever launchd-
// context decision it has on file (typically: denied, no dialog).
//
// By executing this binary (a real Mach-O located inside Contents/MacOS/),
// macOS sees the .app bundle as the originator. The execv'd python3.12
// inherits the responsible-bundle attribution. First mic access then
// triggers a TCC prompt naming "FridayVoice" instead of "python3" — and the
// resulting grant persists against CFBundleIdentifier (com.friday.voice).
#include <unistd.h>

int main(int argc, char **argv) {
    (void)argc;
    (void)argv;
    char *args[] = {
        "/Users/keller/friday/friday/voice/.venv/bin/python",
        "/Users/keller/friday/friday/voice/listen.py",
        (char *)0,
    };
    execv(args[0], args);
    return 127;  // execv only returns on failure
}
