import { DateUnit } from './date-unit.js';
/**
 * All duration units supported by date-vir.
 *
 * @category Unit
 */
export declare enum DurationUnit {
    Years = "years",
    Months = "months",
    Weeks = "weeks",
    Days = "days",
    Hours = "hours",
    Minutes = "minutes",
    Seconds = "seconds",
    Milliseconds = "milliseconds"
}
/**
 * Array of sorted {@link DurationUnit} possibilities: from smallest unit (milliseconds at index 0)
 * to longest unit (years).
 *
 * @category Internal
 */
export declare const orderedDurationUnits: readonly [DurationUnit.Milliseconds, DurationUnit.Seconds, DurationUnit.Minutes, DurationUnit.Hours, DurationUnit.Days, DurationUnit.Weeks, DurationUnit.Months, DurationUnit.Years];
/**
 * The maximum value, exclusive, for each duration unit before it should roll-over into the next
 * duration unit.
 *
 * @category Internal
 */
export declare const maxDurations: Readonly<Record<DurationUnit, number>>;
/**
 * A mapping from {@link DurationUnit} to {@link DateUnit}.
 *
 * @category Unit
 */
export declare const durationUnitToDateUnit: Readonly<Record<DurationUnit, DateUnit>>;
/**
 * A mapping from {@link DateUnit} to {@link DurationUnit}.
 *
 * @category Unit
 */
export declare const dateUnitToDurationUnit: Readonly<Record<DateUnit, DurationUnit>>;
