import { type AnyDuration } from './duration.js';
/**
 * Negates all properties in a duration.
 *
 * @category Duration
 * @example
 *
 * ```ts
 * import {negateDuration} from 'date-vir';
 *
 * negateDuration({hours: -1, minutes: 5, seconds: 35});
 * // `{hours: 1, minutes: -5, seconds: -35}`
 * ```
 */
export declare function negateDuration<const Duration extends Readonly<AnyDuration>>(duration: Duration): Duration;
