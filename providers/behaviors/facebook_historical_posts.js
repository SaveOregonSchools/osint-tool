class FacebookHistoricalPostsBehavior {
  static init() {
    return {
      state: {
        posts: 0,
        expandedPosts: 0,
        attachedImages: 0,
        oldestPostDate: "",
        newestPostDate: "",
        stopReason: "",
      },
    };
  }

  static get id() {
    // Replace Browsertrix's broad Facebook behavior with the timeline-only preset.
    return "Facebook";
  }

  static isMatch() {
    return /^https:\/\/(?:www\.|m\.)?facebook\.com\//i.test(window.location.href);
  }

  constructor() {
    this.seenPosts = new Set();
    this.seenImages = new Set();
  }

  isLoggedIn() {
    return !document.querySelector(
      "form#login_form, form[action*='login'], input[name='pass'], input[type='password']",
    );
  }

  async awaitPageLoad(ctx) {
    const { assertContentValid, waitUntilNode } = ctx.Lib;
    await waitUntilNode("//div[@role='main']", document, null, 15000);
    assertContentValid(() => this.isLoggedIn(), "not_logged_in");
  }

  articles() {
    const candidates = Array.from(
      document.querySelectorAll(
        "div[role='feed'] div[role='article']:not([aria-label]), div[aria-posinset]",
      ),
    );
    return candidates.filter(
      (node, index) =>
        candidates.indexOf(node) === index &&
        !candidates.some((other) => other !== node && other.contains(node)),
    );
  }

  canonicalPostUrl(article) {
    const selectors = [
      "a[href*='/posts/']",
      "a[href*='/permalink/']",
      "a[href*='story_fbid=']",
      "a[href*='/groups/'][href*='/posts/']",
    ];
    for (const selector of selectors) {
      const link = article.querySelector(selector);
      if (!link || !link.href) {
        continue;
      }
      try {
        const url = new URL(link.href, window.location.href);
        for (const key of Array.from(url.searchParams.keys())) {
          if (!new Set(["story_fbid", "id"]).has(key)) {
            url.searchParams.delete(key);
          }
        }
        url.hash = "";
        return url.toString();
      } catch (_error) {
        // Try another permalink selector.
      }
    }
    return "";
  }

  postIdentity(article) {
    const url = this.canonicalPostUrl(article);
    const match = url.match(
      /\/(?:posts|permalink)\/([^/?#]+)|[?&]story_fbid=([^&#]+)|\/groups\/[^/]+\/posts\/([^/?#]+)/i,
    );
    if (match) {
      return { key: match[1] || match[2] || match[3], url };
    }
    const date = this.postDate(article);
    const text = (article.innerText || article.textContent || "")
      .replace(/\s+/g, " ")
      .trim()
      .slice(0, 240);
    return { key: `${date}|${text}`, url };
  }

  postDate(article) {
    const time = article.querySelector("time[datetime]");
    if (time) {
      const parsed = new Date(time.getAttribute("datetime"));
      if (!Number.isNaN(parsed.getTime())) {
        return parsed.toISOString();
      }
    }
    const unixTime = article.querySelector("abbr[data-utime], [data-utime]");
    if (unixTime) {
      const seconds = Number(unixTime.getAttribute("data-utime"));
      if (Number.isFinite(seconds) && seconds > 0) {
        return new Date(seconds * 1000).toISOString();
      }
    }
    return "";
  }

  updateDateRange(ctx, value) {
    if (!value) {
      return;
    }
    if (!ctx.state.oldestPostDate || value < ctx.state.oldestPostDate) {
      ctx.state.oldestPostDate = value;
    }
    if (!ctx.state.newestPostDate || value > ctx.state.newestPostDate) {
      ctx.state.newestPostDate = value;
    }
  }

  async expandPost(article, sleep) {
    const candidates = Array.from(
      article.querySelectorAll("div[role='button'], button, a[role='button'], span"),
    );
    const clicked = new Set();
    let count = 0;
    for (const candidate of candidates) {
      const label = (candidate.innerText || candidate.textContent || "")
        .replace(/\s+/g, " ")
        .trim();
      if (!/^(?:see|show) more$/i.test(label)) {
        continue;
      }
      const button = candidate.closest("div[role='button'], button, a[role='button']") || candidate;
      if (clicked.has(button) || !article.contains(button)) {
        continue;
      }
      clicked.add(button);
      button.click();
      count += 1;
      await sleep(350);
    }
    return count;
  }

  isAttachedImage(image) {
    const source = image.currentSrc || image.src || "";
    if (!/^https?:\/\//i.test(source) || this.seenImages.has(source)) {
      return false;
    }
    const description = `${image.alt || ""} ${source}`.toLowerCase();
    if (/(?:profile picture|avatar|emoji|static_map|safe_image)/.test(description)) {
      return false;
    }
    const link = image.closest("a[href]");
    if (link) {
      try {
        const target = new URL(link.href, window.location.href);
        if (!/(?:^|\.)facebook\.com$/i.test(target.hostname)) {
          return false;
        }
      } catch (_error) {
        return false;
      }
    }
    const width = image.naturalWidth || image.width || 0;
    const height = image.naturalHeight || image.height || 0;
    return width >= 240 && height >= 180 && width * height >= 60000;
  }

  async captureAttachedImages(article, sleep) {
    let count = 0;
    for (const image of Array.from(article.querySelectorAll("img"))) {
      image.scrollIntoView({ block: "center", inline: "nearest" });
      await sleep(150);
      if (!this.isAttachedImage(image)) {
        continue;
      }
      const source = image.currentSrc || image.src;
      this.seenImages.add(source);
      try {
        await fetch(source, { mode: "no-cors", credentials: "include", cache: "force-cache" });
      } catch (_error) {
        // The browser's normal image request may already have been recorded.
      }
      count += 1;
    }
    return count;
  }

  disableVideo() {
    for (const video of document.querySelectorAll("video")) {
      video.pause();
      video.removeAttribute("autoplay");
      video.preload = "none";
    }
  }

  async *run(ctx) {
    const { getState, sleep } = ctx.Lib;
    this.disableVideo();
    let stableRounds = 0;

    while (stableRounds < 6) {
      const articles = this.articles();
      let newPosts = 0;
      for (const article of articles) {
        const identity = this.postIdentity(article);
        if (!identity.key || this.seenPosts.has(identity.key)) {
          continue;
        }
        this.seenPosts.add(identity.key);
        article.scrollIntoView({ block: "center", inline: "nearest" });
        await sleep(500);
        this.disableVideo();
        ctx.state.expandedPosts += await this.expandPost(article, sleep);
        ctx.state.attachedImages += await this.captureAttachedImages(article, sleep);
        this.updateDateRange(ctx, this.postDate(article));
        ctx.state.posts = this.seenPosts.size;
        newPosts += 1;
        yield getState(
          ctx,
          `facebook_historical_post post=${identity.key} url=${identity.url || "unknown"}`,
        );
      }

      stableRounds = newPosts ? 0 : stableRounds + 1;
      const lastArticle = articles.at(-1);
      if (lastArticle) {
        lastArticle.scrollIntoView({ block: "end", inline: "nearest" });
      }
      window.scrollTo(0, document.documentElement.scrollHeight);
      await sleep(newPosts ? 1800 : 3000);
    }

    ctx.state.stopReason = "no_new_unique_posts_after_6_scrolls";
    yield getState(
      ctx,
      `facebook_historical_summary posts=${ctx.state.posts} expanded=${ctx.state.expandedPosts} images=${ctx.state.attachedImages} oldest=${ctx.state.oldestPostDate || "unknown"} newest=${ctx.state.newestPostDate || "unknown"} stop=${ctx.state.stopReason}`,
    );
  }
}
