"use client";

import { createContext, useCallback, useContext, useEffect, useState } from "react";
import { NextIntlClientProvider } from "next-intl";
import { useRouter } from "next/navigation";

import { messagesFor } from "./messages";
import { writeLanguageCookie, type AppLocale, type LanguagePreference } from "./negotiate";

const LanguageContext = createContext<{
  browserLocale: AppLocale;
  setLanguage: (language: LanguagePreference) => void;
} | null>(null);

export function LanguageProvider({ locale: initialLocale, browserLocale, children }: {
  locale: AppLocale;
  browserLocale: AppLocale;
  children: React.ReactNode;
}) {
  const [locale, setLocale] = useState(initialLocale);
  const router = useRouter();
  useEffect(() => setLocale(initialLocale), [initialLocale]);
  const setLanguage = useCallback((language: LanguagePreference) => {
    const next = language === "auto" ? browserLocale : language;
    writeLanguageCookie(language);
    document.documentElement.lang = next;
    setLocale(next);
    router.refresh();
  }, [browserLocale, router]);

  return (
    <LanguageContext.Provider value={{ browserLocale, setLanguage }}>
      <NextIntlClientProvider locale={locale} messages={messagesFor(locale)}>
        {children}
      </NextIntlClientProvider>
    </LanguageContext.Provider>
  );
}

export function useLanguage() {
  const value = useContext(LanguageContext);
  if (!value) throw new Error("LanguageProvider is required");
  return value;
}
