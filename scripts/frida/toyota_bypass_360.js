/*
 * Toyota OneApp (com.toyota.oneapp v3.6.0, vc 222) — client-side root/emulator bypass.
 * EXPERIMENTAL, not yet verified end-to-end. See git history / PR discussion for the
 * full investigation. 3.6.0 re-ran R8, so every obfuscated name the 3.5.0 bypass
 * (toyota_bypass.js) targeted (tr.g, tr.i/l/n, com.toyota.oneapp.core.v, vr.d) no longer
 * exists under those names and the old hooks silently no-op.
 *
 * Leaf detectors identified by decompiling both the running production base.apk (3.5.0)
 * and a freshly-pulled 3.6.0 base.apk with jadx and diffing by STRUCTURE rather than by
 * name (names reset every build; the "security" Gradle module name survives in Kotlin
 * @Metadata tags though, which is what led here):
 *   ws.a implements vs.j : KeyguardManager.isDeviceSecure() -- LEFT ALONE (real PIN state).
 *   ws.r implements vs.h : emulator (genyd/qemud/vbox86/nox/andy marker files+sockets).
 *   ws.t implements vs.m : install source isn't com.android.vending/Play-family.
 *   ws.v implements vs.k : root-helper-binary (/system/xbin/which, "startup").
 *   ws.w implements vs.k : root-management-app package ids (SuperSU etc).
 *   ws.x implements vs.k : su-binary-path list.
 * These all hook cleanly (confirmed: no "could not hook" warnings, and they fire at
 * runtime) but hooking them ALONE was NOT sufficient -- the "rooted devices" dialog
 * (string res application_rooted_title) still appeared, so whatever actually gates it
 * (vs.b's concrete class, never located by grep -- likely Hilt-synthesized, or a
 * Compose-side check jadx didn't fully decompile) is independent of these leaf detectors.
 *
 * THIS VERSION adds a diagnostic: hook Resources.getString(int) / getString(int, ...)
 * and log a Java stack trace whenever the rooted-dialog string IDs are requested, to find
 * the real caller dynamically (static grep for both the symbolic name AND the inlined int
 * literal came up empty across the whole decompiled tree -- likely because the real call
 * site is inside Compose-compiler-generated code jadx didn't fully resolve).
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

    // Leaf detectors -> force "not detected". ws.a (device-secure/PIN) deliberately left alone.
    hook("ws.r", "a", false);   // emulator
    hook("ws.t", "a", false);   // install source != Play
    hook("ws.v", "a", false);   // root helper binaries
    hook("ws.w", "a", false);   // known root-management apps
    hook("ws.x", "a", false);   // su binary paths

    // vs.g is the concrete vs.b implementor (found live via Java.choose on a running
    // DeviceIntegrityMonitorImpl instance -- jadx failed to decompile it, same as DIM itself).
    // vs.b's 3-method shape (a/b/c) matches the OLD tr.g exactly; SecurityActivity reads
    // deviceIntegrityChecker.b() as "isDeviceCompromised". This is the actual top-level gate --
    // hook it directly rather than relying on leaf detectors alone, since leaf-only hooking
    // (ws.r/t/v/w/x) was already tried and did NOT clear the dialog, meaning vs.g computes its
    // answer some other way too (or in addition). a() is left alone, matching ws.a / the old
    // tr.g.a() convention ("is secure" / real PIN state).
    hook("vs.g", "b", false);
    hook("vs.g", "c", false);

    // Safety net: stable platform API names, unaffected by obfuscation.
    try {
        var Sys = Java.use("java.lang.System");
        Sys.exit.implementation = function (code) { console.log("[bypass] System.exit(" + code + ") blocked"); };
        var P = Java.use("android.os.Process");
        P.killProcess.implementation = function (pid) { console.log("[bypass] killProcess(" + pid + ") blocked"); };
    } catch (e) { console.log("[warn] exit/kill hook: " + e); }

    // THE ACTUAL KILL-SWITCH, found via the Resources.getString trace below: the dialog is
    // shown by an ONGOING monitor, not a one-time splash-time gate. Call chain:
    //   com.toyota.oneapp.features.security.monitor.DeviceIntegrityMonitorImpl$startMonitoring$1
    //     -> xs.a.display() -> xs.a.a(activity) -> Context.getString(application_rooted_title)
    // DeviceIntegrityMonitorImpl's real name survived R8 (unlike everything else here). xs.a
    // implements the xs.f interface (display()/a(Activity)) and is the direct equivalent of the
    // 3.5.0 bypass's vr.d kill-switch -- neuter it the same way, at the actual display step,
    // rather than chasing every detector that might feed into the monitor's decision.
    try {
        var XsA = Java.use("xs.a");
        XsA.display.implementation = function () {
            console.log("[bypass] xs.a.display() suppressed (DeviceIntegrityMonitorImpl kill-switch)");
        };
        XsA.a.overload('android.app.Activity').implementation = function (activity) {
            console.log("[bypass] xs.a.a(activity) suppressed (DeviceIntegrityMonitorImpl kill-switch)");
        };
    } catch (e) { console.log("[warn] xs.a kill-switch hook: " + e); }

    console.log("[bypass-360] leaf-detector + kill-switch hooks installed; tracing Resources.getString for the rooted-dialog IDs.");

    // DIAGNOSTIC: the two string resource IDs for the dialog (application_rooted_title /
    // application_rooted_description, decimal from aapt). Log a stack trace whenever either
    // is requested, from WHATEVER caller -- this finds the real trigger even if it's buried
    // in Compose-generated code that jadx couldn't decompile cleanly.
    var ROOTED_IDS = [2132019069, 2132019068];
    try {
        var Res = Java.use("android.content.res.Resources");
        var Thr = Java.use("java.lang.Throwable");
        function maybeLog(id, where) {
            if (ROOTED_IDS.indexOf(id) !== -1) {
                console.log("[TRACE] Resources." + where + "(" + id + ") called. Stack:");
                console.log(Thr.$new("trace").getStackTrace().join("\n"));
            }
        }
        Res.getString.overload('int').implementation = function (id) {
            maybeLog(id, "getString/1");
            return this.getString(id);
        };
        Res.getString.overload('int', '[Ljava.lang.Object;').implementation = function (id, args) {
            maybeLog(id, "getString/2");
            return this.getString(id, args);
        };
        Res.getText.overload('int').implementation = function (id) {
            maybeLog(id, "getText/1");
            return this.getText(id);
        };
    } catch (e) { console.log("[warn] Resources trace hook: " + e); }

    // Also trace via Context.getString (common shortcut that delegates to Resources, but
    // catch it directly too in case the delegation path is non-obvious).
    try {
        var Ctx = Java.use("android.content.Context");
        var Thr2 = Java.use("java.lang.Throwable");
        Ctx.getString.overload('int').implementation = function (id) {
            if ([2132019069, 2132019068].indexOf(id) !== -1) {
                console.log("[TRACE] Context.getString(" + id + ") called. Stack:");
                console.log(Thr2.$new("trace").getStackTrace().join("\n"));
            }
            return this.getString(id);
        };
    } catch (e) { console.log("[warn] Context trace hook: " + e); }

    // DIAGNOSTIC: jadx silently failed to decompile DeviceIntegrityMonitorImpl (probably a
    // complex Kotlin coroutine state machine it couldn't resolve). It demonstrably exists and
    // runs though, so inspect it live via reflection instead of static decompilation.
    try {
        var DIM = Java.use("com.toyota.oneapp.features.security.monitor.DeviceIntegrityMonitorImpl");
        var cls = DIM.class;
        console.log("[reflect] declared methods of DeviceIntegrityMonitorImpl:");
        cls.getDeclaredMethods().forEach(function (m) { console.log("  " + m.toString()); });
        console.log("[reflect] declared fields:");
        cls.getDeclaredFields().forEach(function (f) { console.log("  " + f.toString()); });
    } catch (e) { console.log("[warn] DIM reflect: " + e); }

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
            // Find the LIVE concrete classes behind vs.b and fd.a by walking heap instances --
            // ground truth, sidesteps jadx's decompile gap. Delayed so Hilt has had time to
            // actually construct these (Java.choose finds instances, not just loaded classes).
            console.log("[diag] typeof Java.choose = " + typeof Java.choose + ", typeof Java.enumerateInstances = " + typeof Java.enumerateInstances);
            try {
                Java.choose("com.toyota.oneapp.features.security.monitor.DeviceIntegrityMonitorImpl", {
                    onMatch: function (instance) {
                        try {
                            var cls = instance.getClass();
                            var fB = cls.getDeclaredField("b"); fB.setAccessible(true);
                            var fE = cls.getDeclaredField("e"); fE.setAccessible(true);
                            var vB = fB.get(instance);
                            var vE = fE.get(instance);
                            console.log("[choose] DIM instance found. field b (vs.b) concrete class = " + (vB ? vB.getClass().getName() : "null"));
                            console.log("[choose] DIM instance found. field e (fd.a) concrete class = " + (vE ? vE.getClass().getName() : "null"));
                        } catch (e2) { console.log("[warn] field read: " + e2); }
                    },
                    onComplete: function () { console.log("[choose] DIM scan complete"); }
                });
            } catch (e) { console.log("[warn] choose DIM: " + e); }
        });
    }, 8000);
});
