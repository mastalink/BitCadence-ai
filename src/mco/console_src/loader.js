document.addEventListener('DOMContentLoaded', async function() {
  const loading = document.getElementById('__bundler_loading');
  function setStatus(msg) { if (loading) loading.textContent = msg; }

  // Keep reporting errors after the console shell replaces the loading screen.
  window.addEventListener('error', function(e) {
    var p = document.body || document.documentElement;
    var d = document.getElementById('__bundler_err') || p.appendChild(document.createElement('div'));
    d.id = '__bundler_err';
    d.style.cssText = 'position:fixed;bottom:12px;left:12px;right:12px;font:12px/1.4 ui-monospace,monospace;background:#2a1215;color:#ff8a80;padding:10px 14px;border-radius:8px;border:1px solid #5c2b2e;z-index:99999;white-space:pre-wrap;max-height:40vh;overflow:auto';
    d.textContent = (d.textContent ? d.textContent + String.fromCharCode(10) : '') +
      '[bundle] ' + (e.message || e.type) +
      (e.filename ? ' (' + e.filename.slice(0, 60) + ':' + e.lineno + ')' : '');
  }, true);

  try {
    const manifestEl = document.querySelector('script[type="__bundler/manifest"]');
    const head = document.getElementById('__bundler_head');
    const body = document.getElementById('__bundler_body');
    if (!manifestEl || !head || !body) {
      setStatus('Error: missing bundle data');
      return;
    }

    const manifest = JSON.parse(manifestEl.textContent);

    const uuids = Object.keys(manifest);
    setStatus('Unpacking ' + uuids.length + ' assets...');

    const blobUrls = {};
    await Promise.all(uuids.map(async (uuid) => {
      const entry = manifest[uuid];
      try {
        const binaryStr = atob(entry.data);
        const bytes = new Uint8Array(binaryStr.length);
        for (let i = 0; i < binaryStr.length; i++) bytes[i] = binaryStr.charCodeAt(i);

        let finalBytes = bytes;
        if (entry.compressed) {
          if (typeof DecompressionStream !== 'undefined') {
            const ds = new DecompressionStream('gzip');
            const writer = ds.writable.getWriter();
            const reader = ds.readable.getReader();
            writer.write(bytes);
            writer.close();
            const chunks = [];
            let totalLen = 0;
            while (true) {
              const { done, value } = await reader.read();
              if (done) break;
              chunks.push(value);
              totalLen += value.length;
            }
            finalBytes = new Uint8Array(totalLen);
            let offset = 0;
            for (const chunk of chunks) { finalBytes.set(chunk, offset); offset += chunk.length; }
          } else {
            console.warn('DecompressionStream not available, asset ' + uuid + ' may not render');
          }
        }

        blobUrls[uuid] = URL.createObjectURL(new Blob([finalBytes], { type: entry.mime }));
      } catch (err) {
        console.error('Failed to decode asset ' + uuid + ':', err);
        blobUrls[uuid] = URL.createObjectURL(new Blob([], { type: entry.mime }));
      }
    }));

    const extResEl = document.querySelector('script[type="__bundler/ext_resources"]');
    const extResources = extResEl ? JSON.parse(extResEl.textContent) : [];
    const resourceMap = {};
    for (const entry of extResources) {
      if (blobUrls[entry.uuid]) resourceMap[entry.id] = blobUrls[entry.uuid];
    }

    setStatus('Rendering...');
    window.__resources = resourceMap;
    const headContent = head.content.cloneNode(true);
    const bodyContent = body.content.cloneNode(true);
    for (const style of headContent.querySelectorAll('style')) {
      for (const uuid of uuids) {
        style.textContent = style.textContent.split(uuid).join(blobUrls[uuid]);
      }
    }

    // The browser parses the static templates with the document. Install
    // scripts from the bundled asset manifest in their original order.
    const placeholders = Array.from(bodyContent.querySelectorAll('script[data-asset]'));
    document.documentElement.lang = 'en';
    document.head.replaceChildren(headContent);
    document.body.replaceChildren(bodyContent);
    for (const placeholder of placeholders) {
      const url = blobUrls[placeholder.dataset.asset];
      if (!url) throw new Error('Missing bundled script: ' + placeholder.dataset.asset);
      const script = document.createElement('script');
      if (placeholder.type === 'text/babel' || placeholder.type === 'text/jsx') {
        script.type = placeholder.type;
        const response = await fetch(url);
        script.textContent = await response.text();
        placeholder.replaceWith(script);
      } else {
        const loaded = new Promise((resolve, reject) => {
          script.onload = resolve;
          script.onerror = () => reject(new Error('Unable to load bundled script'));
        });
        script.src = url;
        placeholder.replaceWith(script);
        await loaded;
      }
    }
    // Babel standalone auto-transforms type=text/babel on DOMContentLoaded,
    // which fired before we swapped the document. Trigger manually if present.
    if (window.Babel && typeof window.Babel.transformScriptTags === 'function') {
      window.Babel.transformScriptTags();
    }
  } catch (err) {
    setStatus('Error unpacking: ' + err.message);
    console.error('Bundle unpack error:', err);
  }
});
