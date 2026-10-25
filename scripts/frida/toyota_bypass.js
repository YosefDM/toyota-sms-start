/*
 * Toyota OneApp (com.toyota.oneapp v3.6.0, vc 222) — client-side root/emulator bypass.
 * VERIFIED end-to-end on 2026-10-25: reaches OADashboardActivity, logged in, session intact,
 * all 9 remote command resource-ids unchanged from 3.5.0 (only this bypass needed updating).
 *
 * 3.6.0 re-ran R8, so every obfuscated name the 3.5.0 bypass (toyota_bypass_350.js) targeted
 * (tr.g, tr.i/l/n, com.toyota.oneapp.core.v, vr.d) no longer exists under those names and the
 * old hooks silently no-op -- the app shows "does not support rooted devices" exactly as if no
 * bypass were attached. Found by decompiling both the 3.5.0 and 3.6.0 base.apk with jadx and
 * diffing by STRUCTURE rather than by name (names reset every build; the "security" Gradle
 * module name survives in Kotlin @Metadata tags though, which is what led here). jadx failed to
 * decompile the two classes that matter most (complex Kotlin coroutine state machines) -- those
 * were identified live instead, via Frida reflection and Java.choose on running instances.
 *
 * Architecture (package `vs` = detector interfaces, `ws` = concrete leaf detectors):
 *   vs.g implements vs.b : the top-level checker (3-method a/b/c shape, same as the OLD tr.g).
 *                   SecurityActivity reads deviceIntegrityChecker.b() as "isDeviceCompromised".
 *                   Found via Java.choose on a live DeviceIntegrityMonitorImpl instance's own
 *                   `vs.b`-typed field -- jadx produced an empty file for vs.g, same decompile
 *                   failure as DeviceIntegrityMonitorImpl itself.
 *   ws.a implements vs.j : KeyguardManager.isDeviceSecure() -- LEFT ALONE (real PIN state),
 *                   same convention as the old tr.g.a().
 *   ws.r/t/v/w/x  : leaf detectors (emulator / install-source / root-helper-binary /
 *                   root-management-app / su-path). Hooking these ALONE was NOT sufficient --
 *                   vs.g computes its answer some other way too -- so vs.g is hooked directly;
 *                   the leaf hooks stay as defense in depth, same layered approach as 3.5.0.
 *   DeviceIntegrityMonitorImpl (real name survived R8) -> xs.a.display()/xs.a.a(Activity) is
 *                   the actual dialog-and-kill path, an ONGOING monitor rather than a one-time
 *                   splash-time gate. It's the direct equivalent of the 3.5.0 bypass's vr.d
 *                   kill-switch. NOT hooked here: with vs.g already returning "not compromised",
 *                   the monitor never calls it, and neutering it directly (tried first) left the
 *                   app on a blank screen after a successful-looking dialog suppression -- some
 *                   downstream state apparently expects it to actually run. Hooking the decision
 *                   (vs.g) instead of the display call avoided that entirely.
 */
Java.perform(function () {
    function hook(cls, method, retVal) {
        try {
            var C = Java.use(cls);
            C[method].overloads.forEach(function (ov) {
                ov.implementation = function () {
                    console.log("[bypass] " + cls + "." + method + "() -> " + retVal);
                    return retVal;
                };
            });
        } catch (e) {
            console.log("[warn] could not hook " + cls + "." + method + ": " + e);
        }
    }

    // The actual top-level gate. a() ("is secure") is deliberately left alone.
    hook("vs.g", "b", false);
    hook("vs.g", "c", false);

    // Leaf detectors -> force "not detected", defense in depth. ws.a (device-secure/PIN) is
    // deliberately NOT touched, same intent as the old script: let the app read the real PIN.
    hook("ws.r", "a", false);   // emulator (genyd/qemud/vbox86/nox/andy marker files+sockets)
    hook("ws.t", "a", false);   // install source isn't com.android.vending/Play-family
    hook("ws.v", "a", false);   // root helper binaries (/system/xbin/which, "startup")
    hook("ws.w", "a", false);   // known root-management apps (SuperSU etc, by package id)
    hook("ws.x", "a", false);   // su binary paths

    // Safety net: block exit/kill app-wide so a stray check we haven't found can't nuke the
    // app. Stable platform API names -- unaffected by this build's obfuscation.
    try {
        var Sys = Java.use("java.lang.System");
        Sys.exit.implementation = function (code) { console.log("[bypass] System.exit(" + code + ") blocked"); };
        var P = Java.use("android.os.Process");
        P.killProcess.implementation = function (pid) { console.log("[bypass] killProcess(" + pid + ") blocked"); };
    } catch (e) { console.log("[warn] exit/kill hook: " + e); }

    console.log("[bypass-360] vs.g + leaf-detector hooks installed (ws.a left alone).");

    // Enable ForgeRock SDK verbose logging, same as the 3.5.0 script (unrelated vendored SDK,
    // package name not app-obfuscated).
    try {
        var L = Java.use("org.forgerock.android.auth.Logger");
        var Lvl = Java.use("org.forgerock.android.auth.Logger$Level");
        L.set(Lvl.DEBUG.value);
        console.log("[bypass] ForgeRock Logger set to DEBUG");
    } catch (e) { console.log("[warn] FR logger: " + e); }

    // Read-only diagnostic: what does the REAL device report now?
    setTimeout(function () {
        Java.perform(function () {
            try {
                var AT = Java.use("android.app.ActivityThread");
                var ctx = AT.currentApplication().getApplicationContext();
                var KM = Java.use("android.app.KeyguardManager");
                var km = Java.cast(ctx.getSystemService("keyguard"), KM);
                console.log("[probe] REAL isDeviceSecure()=" + km.isDeviceSecure() +
                            " isKeyguardSecure()=" + km.isKeyguardSecure());
            } catch (e) { console.log("[probe] err " + e); }
        });
    }, 3000);
});
