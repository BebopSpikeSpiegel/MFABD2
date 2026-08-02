/**
 * Creates as new sorted object copied from the the original given object.
 *
 * @category Object
 * @category Package : @augment-vir/common
 * @package [`@augment-vir/common`](https://www.npmjs.com/package/@augment-vir/common)
 */
export function sortObject(original, 
/**
 * A custom comparison for the sort. By default, only the keys are sorted with default string
 * sorting.
 */
comparison) {
    return recursivelySortObject(original, new Map(), comparison);
}
function recursivelySortObject(original, seen, comparison) {
    if (original &&
        typeof original === 'object' &&
        !Array.isArray(original) &&
        original.constructor === Object) {
        if (seen.has(original)) {
            return seen.get(original);
        }
        /** Immediately store in {@link seen} so any circular references can immediately reuse this. */
        const sortedClone = {};
        seen.set(original, sortedClone);
        Object.entries(original)
            .sort((a, b) => {
            if (comparison) {
                return comparison({
                    key: a[0],
                    value: a[1],
                }, {
                    key: b[0],
                    value: b[1],
                });
            }
            else {
                return a[0].localeCompare(b[0]);
            }
        })
            .forEach(([key, value,]) => {
            const mappedValue = recursivelySortObject(value, seen, comparison);
            sortedClone[key] = mappedValue;
        });
        return sortedClone;
    }
    else {
        return original;
    }
}
