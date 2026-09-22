(() => {
  const images = [...document.querySelectorAll('img[data-video-thumbnail]')];
  if (!images.length) return;

  const queue = [];
  const objectUrls = [];
  const controller = new AbortController();
  let running = false;
  let leaving = false;

  const delay = ms => new Promise(resolve => window.setTimeout(resolve, ms));

  async function load(image) {
    for (let attempt = 0; attempt < 15 && !leaving; attempt++) {
      try {
        const response = await fetch(image.dataset.videoThumbnail, {signal: controller.signal});
        if (response.status === 202) {
          await delay(1500);
          continue;
        }
        if (!response.ok) return;
        const url = URL.createObjectURL(await response.blob());
        if (leaving) { URL.revokeObjectURL(url); return; }
        objectUrls.push(url);
        image.src = url;
        image.hidden = false;
      } catch (_) {
        // A missing FFmpeg installation or an unsupported file leaves the fallback visible.
      }
      return;
    }
  }

  async function drain() {
    if (running) return;
    running = true;
    while (queue.length && !leaving) await load(queue.shift());
    running = false;
  }

  const observer = new IntersectionObserver(entries => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      observer.unobserve(entry.target);
      queue.push(entry.target.querySelector('img[data-video-thumbnail]'));
    }
    drain();
  }, {rootMargin: '250px'});
  images.forEach(image => observer.observe(image.closest('.video-card')));

  window.addEventListener('pagehide', () => {
    leaving = true;
    controller.abort();
    observer.disconnect();
    objectUrls.forEach(url => URL.revokeObjectURL(url));
  });
})();
