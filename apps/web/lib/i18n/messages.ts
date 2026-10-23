import zhAuth from "../../locales/zh-Hans/auth.json";
import enAuth from "../../locales/en/auth.json";
import zhSetup from "../../locales/zh-Hans/setup.json";
import enSetup from "../../locales/en/setup.json";
import zhCommon from "../../locales/zh-Hans/common.json";
import enCommon from "../../locales/en/common.json";
import zhSettings from "../../locales/zh-Hans/settings.json";
import enSettings from "../../locales/en/settings.json";
import type { AppLocale } from "./negotiate";

export const zhHans = { auth: zhAuth, setup: zhSetup, common: zhCommon, settings: zhSettings };
const en = { auth: enAuth, setup: enSetup, common: enCommon, settings: enSettings };

// Merge by leaf, keeping the Chinese source as a migration fallback.
function merge(source: Record<string, unknown>, translated: Record<string, unknown>): Record<string, unknown> {
  return Object.fromEntries(Object.entries(source).map(([key, value]) => [key,
    typeof value === "object" && value !== null
      ? merge(value as Record<string, unknown>, (translated[key] ?? {}) as Record<string, unknown>)
      : translated[key] ?? value,
  ]));
}

export function messagesFor(locale: AppLocale) {
  return locale === "zh-Hans" ? zhHans : merge(zhHans, en) as typeof zhHans;
}

declare module "next-intl" {
  interface AppConfig {
    Locale: AppLocale;
    Messages: typeof zhHans;
  }
}
