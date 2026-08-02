/**
 * The current user's default locale, useful for formatting purposes.
 *
 * @category Util
 */
export const userLocale = new Intl.DateTimeFormat().resolvedOptions().locale;
