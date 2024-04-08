/*
 * Monkeypatch URLSearchParams
 *
 * Sphinx documents that use `searchtool.js` rely on passing information via
 * GET parameters (aka search parameters). Unfortunately, this doesn't work
 * in our approach due to the same origin policy, so we have to get ...
 * creative.
 *
 * Here, we patch the `URLSearchParams` class so it returns the information
 * stored in `window.global_context.get_parameters`.
 *
 */

const originalGet = URLSearchParams.prototype.get;

var myGet = function (arg) {
    const originalResult = originalGet.apply(this, [arg]);
    // If searchtools.js of sphinx is used
    if (
        window.global_context &&
        window.global_context.get_parameters &&
        (window.location.search === "") &&
        (Array.from(this.entries()).length == 0)
    ) {
        const params = new URLSearchParams('?' + window.global_context.get_parameters);
        const result = params.get(arg);
        // console.log("Return virtual get parameter:", arg, result);
        return result;
    } else {
        return originalResult;
    }
};

var myDelete = function (arg) {};

URLSearchParams.prototype.get = myGet;
URLSearchParams.prototype.delete = myDelete;

/*
 * Monkeypatch window.history
 */

var myReplaceState = function (arg1, arg2, arg3) {};
window.history.replaceState = myReplaceState;

/*
 * Monkeypatch window.fetch
 */

const { fetch: originalFetch } = window;

async function waitFor(predicate, timeout) {
  return new Promise((resolve, reject) => {
    const check = () => {
      console.log('checking', predicate());
      if (!predicate()) return;
      clearInterval(interval);
      resolve();
    };
    const interval = setInterval(check, 100);
    check();

    if (!timeout) return;
    setTimeout(() => {
      clearInterval(interval);
      reject();
    }, timeout);
  });
}

var _base64ToArrayBuffer = function (base64) {
    if (!base64) { return []}
    var binary_string = window.atob(base64);
    var len = binary_string.length;
    var bytes = new Uint8Array(len);
    for (var i = 0; i < len; i++) {
        bytes[i] = binary_string.charCodeAt(i);
    }
    return bytes.buffer;
};

window.fetch = async (...args) => {
    // wait until global_context is ready
    try {
        await waitFor(() => window.hasOwnProperty("global_context"), 10000);
    } catch (err) {
        throw err;
    }

    let [resource, config ] = args;
    var path = normalize_path(resource);
    var response;
    if (is_virtual(path)) {
        var file = retrieve_file(path);
        var data = file.data;
        if (file.base64encoded) {
            data = _base64ToArrayBuffer(data);
        }
        response = new Response(data);
        response.headers.set("content-type", file.mime_type);
    } else {
        response = await originalFetch(resource, config);
    }
    return response;
};
