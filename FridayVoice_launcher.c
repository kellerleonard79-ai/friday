// Minimal Mach-O launcher for the FridayVoice .app bundle.
//
// WHY A COMPILED LAUNCHER AT ALL
// macOS TCC tracks microphone permission against the "responsible code" of the
// requesting process. A #!/bin/bash shell-script launcher fails to attach the
// bundle as the responsible code, so TCC matches on the bare python codesign
// identity and silently inherits whatever launchd-context decision it has on
// file (typically: denied, no dialog).
//
// By executing this binary (a real Mach-O located inside Contents/MacOS/),
// macOS sees the .app bundle as the originator. The execv'd python inherits
// the responsible-bundle attribution. First mic access then triggers a TCC
// prompt naming "FridayVoice" instead of "python3" — and the resulting grant
// persists against CFBundleIdentifier (com.friday.voice).
//
// WHY NO HARDCODED PATHS
// This file used to exec an absolute path under one developer's home
// directory, which meant the shipped bundle could never launch on anyone
// else's machine. Paths are now resolved at runtime, in this order:
//
//   1. <bundle>/Contents/Resources/voice_launcher.conf   (shipped in the .dmg)
//   2. $HOME/.friday/voice_launcher.conf                 (written by setup)
//   3. Bundle-relative defaults, for a self-contained .app
//
// The conf file is two lines: interpreter path, then script path. Setup writes
// it; see friday/macos_setup.py::write_voice_launcher_conf.

#include <limits.h>
#include <mach-o/dyld.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

// Strip the trailing "/Contents/MacOS/<executable>" to get the .app root.
// Returns 0 on success.
static int bundle_root(char *out, size_t out_size) {
    char exec_path[PATH_MAX];
    uint32_t size = sizeof(exec_path);
    if (_NSGetExecutablePath(exec_path, &size) != 0) {
        return -1;
    }
    char resolved[PATH_MAX];
    if (realpath(exec_path, resolved) == NULL) {
        // Fall back to the unresolved path rather than giving up.
        snprintf(resolved, sizeof(resolved), "%s", exec_path);
    }
    // Walk up three components: <root>/Contents/MacOS/FridayVoice
    for (int i = 0; i < 3; i++) {
        char *slash = strrchr(resolved, '/');
        if (slash == NULL) {
            return -1;
        }
        *slash = '\0';
    }
    if (snprintf(out, out_size, "%s", resolved) < 0) {
        return -1;
    }
    return 0;
}

// Read two newline-terminated paths from `path` into interp/script.
// Returns 0 when both lines were present and non-empty.
static int read_conf(const char *path, char *interp, size_t interp_size,
                     char *script, size_t script_size) {
    FILE *f = fopen(path, "r");
    if (f == NULL) {
        return -1;
    }
    int ok = 0;
    if (fgets(interp, (int)interp_size, f) != NULL &&
        fgets(script, (int)script_size, f) != NULL) {
        interp[strcspn(interp, "\r\n")] = '\0';
        script[strcspn(script, "\r\n")] = '\0';
        ok = (interp[0] != '\0' && script[0] != '\0') ? 0 : -1;
    } else {
        ok = -1;
    }
    fclose(f);
    return ok;
}

int main(int argc, char **argv) {
    (void)argc;
    (void)argv;

    char interp[PATH_MAX] = {0};
    char script[PATH_MAX] = {0};
    char root[PATH_MAX] = {0};
    char conf[PATH_MAX] = {0};

    int have_root = (bundle_root(root, sizeof(root)) == 0);

    // 1. Conf shipped inside the bundle.
    if (have_root) {
        snprintf(conf, sizeof(conf), "%s/Contents/Resources/voice_launcher.conf",
                 root);
        if (read_conf(conf, interp, sizeof(interp), script, sizeof(script)) == 0) {
            execv(interp, (char *[]){interp, script, (char *)0});
        }
    }

    // 2. Conf written into the user's data dir by setup.
    const char *home = getenv("HOME");
    if (home != NULL) {
        snprintf(conf, sizeof(conf), "%s/.friday/voice_launcher.conf", home);
        if (read_conf(conf, interp, sizeof(interp), script, sizeof(script)) == 0) {
            execv(interp, (char *[]){interp, script, (char *)0});
        }
    }

    // 3. Bundle-relative defaults for a fully self-contained .app.
    if (have_root) {
        snprintf(interp, sizeof(interp), "%s/Contents/Resources/venv/bin/python3",
                 root);
        snprintf(script, sizeof(script), "%s/Contents/Resources/voice/listen.py",
                 root);
        execv(interp, (char *[]){interp, script, (char *)0});
    }

    fprintf(stderr,
            "FridayVoice: no interpreter found. Expected a two-line "
            "voice_launcher.conf (interpreter, then script) in the bundle's "
            "Resources or in ~/.friday/. Re-run Friday setup to regenerate it.\n");
    return 127;  // execv only returns on failure
}
