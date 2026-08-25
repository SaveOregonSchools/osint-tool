class XAuthPreflightBehavior {
  static init() {
    return { state: {} };
  }

  static get id() {
    // Replace Browsertrix's built-in X/Twitter behavior for this one-page check.
    return "Twitter";
  }

  static isMatch() {
    return /^https:\/\/(?:www\.)?(?:x|twitter)\.com\//i.test(window.location.href);
  }

  explicitLoginIsVisible() {
    if (/\/i\/flow\/login(?:\/|$)/i.test(window.location.pathname)) {
      return true;
    }
    return Boolean(document.querySelector('input[name="password"], form[action*="/i/flow/login"]'));
  }

  loginPromptIsVisible() {
    const labels = Array.from(document.querySelectorAll('a, button, [role="button"]')).map((node) =>
      (node.textContent || "").trim().toLowerCase(),
    );
    const hasLogin = labels.some((label) => /^(?:log in|sign in)$/.test(label));
    const hasSignup = labels.some((label) => /^(?:sign up|create account|join today)$/.test(label));
    return hasLogin && hasSignup;
  }

  authenticatedIdentity() {
    const profileLink = document.querySelector('a[data-testid="AppTabBar_Profile_Link"]');
    const accountSwitcher = document.querySelector('[data-testid="SideNav_AccountSwitcher_Button"]');
    if (!profileLink || !accountSwitcher) {
      return null;
    }
    const reservedPaths = new Set([
      "compose",
      "explore",
      "home",
      "i",
      "login",
      "messages",
      "notifications",
      "search",
      "settings",
    ]);
    let handle = "";
    try {
      const profileUrl = new URL(profileLink.getAttribute("href") || profileLink.href, window.location.origin);
      const candidate = decodeURIComponent(profileUrl.pathname.split("/").filter(Boolean)[0] || "").replace(
        /^@/,
        "",
      );
      if (/^[A-Za-z0-9_]{1,15}$/.test(candidate) && !reservedPaths.has(candidate.toLowerCase())) {
        handle = candidate;
      }
    } catch (_error) {
      // A missing or malformed href is handled by the safe text fallback below.
    }
    if (!handle) {
      const handleMatch = (accountSwitcher.innerText || accountSwitcher.textContent || "").match(
        /@([A-Za-z0-9_]{1,15})(?![A-Za-z0-9_])/,
      );
      handle = handleMatch ? handleMatch[1] : "";
    }
    return { handle };
  }

  async awaitPageLoad(ctx) {
    const deadline = Date.now() + 10000;
    while (Date.now() < deadline) {
      const identity = this.authenticatedIdentity();
      if (identity) {
        await ctx.log({
          msg: `x_auth_preflight_verified${identity.handle ? ` handle=@${identity.handle}` : ""}`,
          authVerified: true,
          handle: identity.handle,
        });
        return;
      }
      if (this.explicitLoginIsVisible()) {
        await ctx.log({ msg: "x_auth_preflight_logged_out", authVerified: false }, "error");
        ctx.Lib.assertContentValid(() => false, "x_auth_logged_out");
        return;
      }
      await ctx.Lib.sleep(250);
    }

    if (this.loginPromptIsVisible()) {
      await ctx.log({ msg: "x_auth_preflight_logged_out", authVerified: false }, "error");
      ctx.Lib.assertContentValid(() => false, "x_auth_logged_out");
    } else {
      await ctx.log({ msg: "x_auth_preflight_indeterminate", authVerified: false }, "error");
      ctx.Lib.assertContentValid(() => false, "x_auth_indeterminate");
    }
  }

  async *run() {
    yield { msg: "x_auth_preflight_complete" };
  }
}
