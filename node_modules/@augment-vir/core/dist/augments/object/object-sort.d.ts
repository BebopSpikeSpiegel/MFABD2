import { type AnyObject } from './generic-object-type.js';
import { type Values } from './object-value-types.js';
/**
 * Optional comparison parameter type for {@link sortObject}.
 *
 * @category Object
 * @category Package : @augment-vir/common
 * @package [`@augment-vir/common`](https://www.npmjs.com/package/@augment-vir/common)
 */
export type SortObjectComparison<T extends AnyObject> = (a: {
    key: keyof T;
    value: Values<T>;
}, b: {
    key: keyof T;
    value: Values<T>;
}) => number;
/**
 * Creates as new sorted object copied from the the original given object.
 *
 * @category Object
 * @category Package : @augment-vir/common
 * @package [`@augment-vir/common`](https://www.npmjs.com/package/@augment-vir/common)
 */
export declare function sortObject<const T extends AnyObject>(original: Readonly<T>, 
/**
 * A custom comparison for the sort. By default, only the keys are sorted with default string
 * sorting.
 */
comparison?: SortObjectComparison<T> | undefined): T;
