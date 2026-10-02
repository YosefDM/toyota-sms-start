/*
 * Toyota OneApp (com.toyota.oneapp v3.5.0) — MINIMAL client-side root/emulator bypass.
 * ONLY neutralizes Lock #1 (the root/emulator/tamper self-destruct), which genuinely
 * cannot "pass" on a rooted emulator. Does NOT fake the device-PIN check — we want to
 * test whether a REAL, cleanly-set device PIN satisfies the app on its own.
 *
 * Targets (from jadx):
 *   tr.g implements tr.b: b()=compromised(root||emu||tamper), c()=emulator -> force FALSE.
 *   com.toyota.oneapp.core.v implements BuildWrapper; k() debug-bypass -> force TRUE.
 *   vr.d.c(Activity) is the killer: finish()+killProcess()+System.exit(1) -> neuter.
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

    // Lock #1 only: not-compromised, not-emulator. (a() = "is secure" is LEFT ALONE
    // so the app reads the real device PIN state.)
    hook("tr.g", "b", false);
    hook("tr.g", "c", false);

    // Debug master-bypass BuildWrapper.k() -> true (short-circuits the compromised check)
    hook("com.toyota.oneapp.core.v", "k", true);

    // Individual root/emulator/tamper detectors -> false
    ["tr.i", "tr.l", "tr.n"].forEach(function (c) { hook(c, "a", false); });

    // Neuter the killer dialog's exit path, and block exit/kill app-wide (safety net
    // so a stray check can't nuke the app; does NOT suppress the PIN dialog itself)
    try {
        var D = Java.use("vr.d");
        D.c.implementation = function (activity) { console.log("[bypass] vr.d.c() kill suppressed"); };
    } catch (e) { console.log("[warn] vr.d.c hook: " + e); }
    try {
        var Sys = Java.use("java.lang.System");
        Sys.exit.implementation = function (code) { console.log("[bypass] System.exit(" + code + ") blocked"); };
        var P = Java.use("android.os.Process");
        P.killProcess.implementation = function (pid) { console.log("[bypass] killProcess(" + pid + ") blocked"); };
    } catch (e) { console.log("[warn] exit/kill hook: " + e); }

    console.log("[bypass] Lock#1-only hooks installed (no PIN faking, no proxy suppression).");

    // Enable ForgeRock SDK verbose logging to expose why sign-in (FRMainActivity) closes
    try {
        var L = Java.use("org.forgerock.android.auth.Logger");
        var Lvl = Java.use("org.forgerock.android.auth.Logger$Level");
        L.set(Lvl.DEBUG.value);
        console.log("[bypass] ForgeRock Logger set to DEBUG");
    } catch (e) { console.log("[warn] FR logger: " + e); }

    // Capture any ForgeRock auth exception/failure
    ["org.forgerock.android.auth.FRListener"].forEach(function(c){
        try {
            var C = Java.use(c);
            if (C.onException) C.onException.implementation = function(ex){
                console.log("[FR-EXCEPTION] " + ex);
                try { console.log(Java.use("android.util.Log").getStackTraceString(ex)); } catch(e){}
                return this.onException(ex);
            };
        } catch(e){}
    });

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
                var S = Java.use("java.lang.System");
                console.log("[probe] http.proxyHost=" + S.getProperty("http.proxyHost") +
                            " http.proxyPort=" + S.getProperty("http.proxyPort") +
                            " https.proxyHost=" + S.getProperty("https.proxyHost"));
            } catch (e) { console.log("[probe] err " + e); }
        });
    }, 3000);
});
