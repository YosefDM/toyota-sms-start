Java.perform(function () {
    var Response = Java.use("okhttp3.Response");
    var ResponseBody = Java.use("okhttp3.ResponseBody");
    var Buffer = Java.use("okio.Buffer");
    Response.body.implementation = function () {
        var body = this.body();
        try {
            var url = this.request().url().toString();
            if (url.indexOf("/remote/route/") >= 0) {
                var src = body.source();
                src.request(1048576);
                var buf = src.getBuffer ? src.getBuffer() : src.buffer();
                var copy = buf.clone();
                var txt = copy.readUtf8();
                console.log("[body] " + this.code() + " " + url + "\n   " + txt.slice(0, 400));
            }
        } catch (e) { /* ignore */ }
        return body;
    };
    console.log("[body] remote-route body logger installed");
});
