import { type AnyDuration } from './duration.js';
import { type LocaleOptions } from './locale.js';
import { type RoundOptions } from './round-options.js';
/**
 * Options for {@link formatDuration}.
 *
 * @category Internal
 */
export type FormatDurationOptions = {
    /**
     * The separator between each unit string.
     *
     * @default ' '
     */
    sep?: string | undefined;
    /**
     * If `true`, only the largest unit with a defined value is included in the output.
     *
     * @default false
     */
    onlyLargestUnit?: boolean | undefined;
    /**
     * If `true`, unit names are abbreviated.
     *
     * @default false
     */
    abbreviate?: boolean | undefined;
} & LocaleOptions & RoundOptions;
/**
 * Formats a duration into a human-readable string by converting each defined unit via
 * {@link getDateUnitString}. Units are ordered from largest to smallest.
 *
 * @category Language
 * @example
 *
 * ```ts
 * import {formatDuration} from '@date-vir/duration';
 *
 * formatDuration({hours: 3, minutes: 30}); // '3 hours, 30 minutes'
 * formatDuration({seconds: 4}); // '4 seconds'
 * formatDuration({days: 1, hours: 2}, {sep: ' and '}); // '1 day and 2 hours'
 * ```
 */
export declare function formatDuration(duration: Readonly<AnyDuration>, options?: Readonly<FormatDurationOptions>): string;
