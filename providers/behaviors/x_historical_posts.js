class XHistoricalPostsBehavior {
  static init() {
    return {
      state: {
        posts: 0,
        attachedImages: 0,
        stopReason: "",
      },
    };
  }

  static get id() {
    // Replace the broad Twitter/X behavior with search-result scrolling only.
    return "Twitter";
  }

  static isMatch() {
    return /^https:\/\/(?:www\.)?(?:x|twitter)\.com\/search(?:\?|$)/i.test(
      window.location.href,
    );
  }

  constructor() {
    this.seenPosts = new Set();
    this.seenImages = new Set();
  }

  isLoggedIn() {
    if (/\/i\/flow\/login(?:\/|$)/i.test(window.location.pathname)) {
      return false;
    }
    return Boolean(
      document.querySelector(
        "a[data-testid='AppTabBar_Profile_Link'], [data-testid='SideNav_AccountSwitcher_Button']",
      ),
    );
  }

  async awaitPageLoad(ctx) {
    const { assertContentValid, waitUntilNode } = ctx.Lib;
    await waitUntilNode("//*[@data-testid='primaryColumn']", document, null, 15000);
    assertContentValid(() => this.isLoggedIn(), "not_logged_in");
  }

  tweets() {
    return Array.from(document.querySelectorAll("article[data-testid='tweet']"));
  }

  identity(tweet) {
    for (const link of tweet.querySelectorAll("a[href*='/status/']")) {
      const match = (link.getAttribute("href") || "").match(/\/([^/]+)\/status\/(\d+)/);
      if (match) {
        return { key: match[2], handle: match[1], url: `https://x.com/${match[1]}/status/${match[2]}` };
      }
    }
    const time = tweet.querySelector("time[datetime]");
    const text = (tweet.innerText || tweet.textContent || "").replace(/\s+/g, " ").trim().slice(0, 240);
    return { key: `${time ? time.getAttribute("datetime") : ""}|${text}`, handle: "", url: "" };
  }

  disableVideo() {
    for (const video of document.querySelectorAll("video")) {
      video.pause();
      video.removeAttribute("autoplay");
      video.preload = "none";
    }
  }

  async captureAttachedImages(tweet, sleep) {
    let count = 0;
    for (const image of tweet.querySelectorAll("img")) {
      const source = image.currentSrc || image.src || "";
      let isPostImage = false;
      try {
        const url = new URL(source);
        isPostImage = /(?:^|\.)pbs\.twimg\.com$/i.test(url.hostname) && url.pathname.startsWith("/media/");
      } catch (_error) {
        isPostImage = false;
      }
      if (!isPostImage || this.seenImages.has(source)) {
        continue;
      }
      this.seenImages.add(source);
      image.scrollIntoView({ block: "center", inline: "nearest" });
      await sleep(150);
      try {
        await fetch(source, { mode: "no-cors", credentials: "include", cache: "force-cache" });
      } catch (_error) {
        // The rendered image request may already have been recorded.
      }
      count += 1;
    }
    return count;
  }

  async *run(ctx) {
    const { getState, sleep } = ctx.Lib;
    let stableRounds = 0;
    this.disableVideo();

    while (stableRounds < 6) {
      const tweets = this.tweets();
      let newPosts = 0;
      for (const tweet of tweets) {
        const identity = this.identity(tweet);
        if (!identity.key || this.seenPosts.has(identity.key)) {
          continue;
        }
        this.seenPosts.add(identity.key);
        tweet.scrollIntoView({ block: "center", inline: "nearest" });
        await sleep(450);
        this.disableVideo();
        ctx.state.attachedImages += await this.captureAttachedImages(tweet, sleep);
        ctx.state.posts = this.seenPosts.size;
        newPosts += 1;
        yield getState(ctx, `x_historical_post post=${identity.key} url=${identity.url || "unknown"}`);
      }

      stableRounds = newPosts ? 0 : stableRounds + 1;
      const lastTweet = tweets.at(-1);
      if (lastTweet) {
        lastTweet.scrollIntoView({ block: "end", inline: "nearest" });
      }
      window.scrollTo(0, document.documentElement.scrollHeight);
      await sleep(newPosts ? 1500 : 3000);
    }

    ctx.state.stopReason = "no_new_unique_posts_after_6_scrolls";
    yield getState(
      ctx,
      `x_historical_summary posts=${ctx.state.posts} images=${ctx.state.attachedImages} stop=${ctx.state.stopReason}`,
    );
  }
}
