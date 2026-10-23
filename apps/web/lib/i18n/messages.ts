import zhAuth from "../../locales/zh-Hans/auth.json" with { type: "json" };
import enAuth from "../../locales/en/auth.json" with { type: "json" };
import zhSetup from "../../locales/zh-Hans/setup.json" with { type: "json" };
import enSetup from "../../locales/en/setup.json" with { type: "json" };
import zhCommon from "../../locales/zh-Hans/common.json" with { type: "json" };
import enCommon from "../../locales/en/common.json" with { type: "json" };
import zhSettings from "../../locales/zh-Hans/settings.json" with { type: "json" };
import enSettings from "../../locales/en/settings.json" with { type: "json" };
import zhUnits from "../../locales/zh-Hans/units.json" with { type: "json" };
import enUnits from "../../locales/en/units.json" with { type: "json" };
import zhHome from "../../locales/zh-Hans/home.json" with { type: "json" };
import enHome from "../../locales/en/home.json" with { type: "json" };
import type { AppLocale } from "./negotiate.ts";

export const zhHans = { auth: zhAuth, setup: zhSetup, common: zhCommon, settings: zhSettings, units: zhUnits, home: zhHome };
const en = { auth: enAuth, setup: enSetup, common: enCommon, settings: enSettings, units: enUnits, home: enHome };

// Merge by leaf, keeping the Chinese source as a migration fallback.
function merge(source: Record<string, unknown>, translated: Record<string, unknown>): Record<string, unknown> {
  return Object.fromEntries(Object.entries(source).map(([key, value]) => [key,
    typeof value === "object" && value !== null
      ? merge(value as Record<string, unknown>, (translated[key] ?? {}) as Record<string, unknown>)
      : translated[key] ?? value,
  ]));
}

const english = merge(zhHans, en) as typeof zhHans;

export function messagesFor(locale: AppLocale) {
  return locale === "zh-Hans" ? zhHans : english;
}

declare module "next-intl" {
  interface AppConfig {
    Locale: AppLocale;
    Messages: typeof zhHans;
  }
}
