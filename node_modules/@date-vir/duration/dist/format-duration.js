import { getDateUnitString } from './units/date-unit.js';
import { orderedDurationUnits } from './units/duration-unit.js';
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
export function formatDuration(duration, options) {
    const unitStrings = [];
    orderedDurationUnits.toReversed().forEach((unit) => {
        if (options?.onlyLargestUnit && unitStrings.length) {
            return;
        }
        const count = duration[unit];
        if (count) {
            if (options?.onlyLargestUnit) {
                const decimalMultiplier = 10 ** (options.decimalCount || 0);
                const roundedCount = Math.round(count * decimalMultiplier) / decimalMultiplier;
                if (!roundedCount) {
                    return;
                }
            }
            unitStrings.push(getDateUnitString({
                count,
                unit,
                locale: options?.locale,
                abbreviate: options?.abbreviate,
                decimalCount: options?.decimalCount,
            }));
        }
    });
    return unitStrings.join(options?.sep ?? ' ');
}
