Java.perform(function () {
    // Log every okhttp3 request URL + response code / failure (the ForgeRock SDK uses okhttp3)
    try {
        var OkHttpClient = Java.use("okhttp3.OkHttpClient");
        var RealCallCls = null;
        try { RealCallCls = Java.use("okhttp3.internal.connection.RealCall"); } catch (e) {}
        var Call = Java.use("okhttp3.Call");
        // hook newCall to see the request URL
        OkHttpClient.newCall.implementation = function (req) {
            try {
                var url = req.url().toString();
                console.log("[okhttp] -> " + url);
            } catch (e) {}
            return this.newCall(req);
        };
    } catch (e) { console.log("[okhttp] client hook err: " + e); }

    // Response code logging via okhttp3.Response.code
    try {
        var Response = Java.use("okhttp3.Response");
        Response.code.implementation = function () {
            var c = this.code();
            try {
                var u = this.request().url().toString();
                if (u.indexOf("telematic") >= 0 || u.indexOf("toyota") >= 0 || u.indexOf("driverslogin") >= 0 ||
                    u.indexOf("openidm") >= 0 || u.indexOf("forgerock") >= 0 || u.indexOf("am/") >= 0)
                    console.log("[okhttp] <= " + c + "  " + u);
            } catch (e) {}
            return c;
        };
    } catch (e) { console.log("[okhttp] response hook err: " + e); }

    // Log IOExceptions from okhttp (handshake/connect failures)
    ["okhttp3.internal.http.RetryAndFollowUpInterceptor","okhttp3.internal.connection.RealConnection"].forEach(function(cn){
        try {
            var C = Java.use(cn);
        } catch(e){}
    });
    console.log("[okhttp] URL logger installed");
});
