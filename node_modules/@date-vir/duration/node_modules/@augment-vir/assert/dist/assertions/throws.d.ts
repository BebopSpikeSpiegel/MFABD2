import { type MaybePromise, type NarrowToExpected, type PartialWithNullable, type TypedFunction } from '@augment-vir/core';
import { type WaitUntilOptions } from '../guard-types/wait-until-function.js';
declare function isError(this: void, actual: unknown, matchOptions?: ErrorMatchOptions | undefined, failureMessage?: string | undefined): asserts actual is Error;
/**
 * A type that represents possible error matching patterns. This is used by the `.throws` and
 * `isError`, guards in `@augment-vir/assert` as well as `itCases` in `@augment-vir/test`. Each
 * property is optional, and whichever properties are provided will be checked.
 *
 * @category Assert : Util
 * @category Package : @augment-vir/assert
 * @example
 *
 * ```ts
 * import {assert, type ErrorMatchOptions} from '@augment-vir/assert';
 *
 * // define the options
 * const matchOptions: ErrorMatchOptions = {
 *     matchConstructor: Error,
 *     matchMessage: 'some error',
 * };
 *
 * assert.throws(
 *     () => {
 *         throw new Error('some error');
 *     },
 *     // use the options
 *     matchOptions,
 * ); // this assertion will pass
 * ```
 *
 * @package [`@augment-vir/assert`](https://www.npmjs.com/package/@augment-vir/assert)
 */
export type ErrorMatchOptions = PartialWithNullable<{
    /**
     * A string or RegExp that an error's message will be compared with.
     *
     * - If this is a string, the error's message will checked for _containing_ the given string (not
     *   strictly equalling it): `error.message.includes(options.matchMessage)`
     * - If this is a RegExp, the error's message will be tested against it:
     *   `error.message.match(options.matchMessage)`
     *
     * If this property is omitted, the error message won't be checked at all.
     */
    matchMessage: string | RegExp;
    /**
     * A constructor that the error will be compared to with `instanceof`: `error instanceof
     * options.matchConstructor`. If this property is omitted, the error's constructor or
     * inheritance will not be checked.
     */
    matchConstructor: ErrorConstructor | (new (...args: any[]) => Error);
}>;
declare function throws(this: void, callbackOrPromise: TypedFunction<void, never>, matchOptions?: ErrorMatchOptions | undefined, failureMessage?: string | undefined): void;
declare function throws(this: void, callbackOrPromise: TypedFunction<void, Promise<any>> | Promise<any>, matchOptions?: ErrorMatchOptions | undefined, failureMessage?: string | undefined): Promise<void>;
declare function throws(this: void, callback: TypedFunction<void, any>, matchOptions?: ErrorMatchOptions | undefined, failureMessage?: string | undefined): void;
declare function throws(this: void, callback: TypedFunction<void, MaybePromise<any>> | Promise<any>, matchOptions?: ErrorMatchOptions | undefined, failureMessage?: string | undefined): MaybePromise<void>;
declare function throwsCheck(this: void, callbackOrPromise: TypedFunction<void, never>, matchOptions?: ErrorMatchOptions | undefined): boolean;
declare function throwsCheck(this: void, callbackOrPromise: TypedFunction<void, Promise<any>> | Promise<any>, matchOptions?: ErrorMatchOptions | undefined): Promise<boolean>;
declare function throwsCheck(this: void, callback: TypedFunction<void, any>, matchOptions?: ErrorMatchOptions | undefined): boolean;
declare function throwsCheck(this: void, callback: TypedFunction<void, MaybePromise<any>> | Promise<any>, matchOptions?: ErrorMatchOptions | undefined): MaybePromise<boolean>;
declare function throwsAssertWrap(this: void, callbackOrPromise: TypedFunction<void, never>, matchOptions?: ErrorMatchOptions | undefined, failureMessage?: string | undefined): Error;
declare function throwsAssertWrap(this: void, callbackOrPromise: TypedFunction<void, Promise<any>> | Promise<any>, matchOptions?: ErrorMatchOptions | undefined, failureMessage?: string | undefined): Promise<Error>;
declare function throwsAssertWrap(this: void, callback: TypedFunction<void, any>, matchOptions?: ErrorMatchOptions | undefined, failureMessage?: string | undefined): Error;
declare function throwsAssertWrap(this: void, callback: TypedFunction<void, MaybePromise<any>> | Promise<any>, matchOptions?: ErrorMatchOptions | undefined, failureMessage?: string | undefined): MaybePromise<Error>;
declare function throwsCheckWrap(this: void, callbackOrPromise: TypedFunction<void, never>, matchOptions?: ErrorMatchOptions | undefined, failureMessage?: string | undefined): Error | undefined;
declare function throwsCheckWrap(this: void, callbackOrPromise: TypedFunction<void, Promise<any>> | Promise<any>, matchOptions?: ErrorMatchOptions | undefined, failureMessage?: string | undefined): Promise<Error | undefined>;
declare function throwsCheckWrap(this: void, callback: TypedFunction<void, any>, matchOptions?: ErrorMatchOptions | undefined, failureMessage?: string | undefined): Error | undefined;
declare function throwsCheckWrap(this: void, callback: TypedFunction<void, MaybePromise<any>> | Promise<any>, matchOptions?: ErrorMatchOptions | undefined, failureMessage?: string | undefined): MaybePromise<Error | undefined>;
declare function throwsWaitUntil(this: void, callback: TypedFunction<void, any>, options?: WaitUntilOptions | undefined, failureMessage?: string | undefined): Promise<Error>;
declare function throwsWaitUntil(this: void, matchOptions: ErrorMatchOptions, callback: TypedFunction<void, any>, options?: WaitUntilOptions | undefined, failureMessage?: string | undefined): Promise<Error>;
declare function doesNotThrow(this: void, callbackOrPromise: TypedFunction<void, never>, failureMessage?: string | undefined): void;
declare function doesNotThrow(this: void, callbackOrPromise: TypedFunction<void, Promise<any>> | Promise<any>, failureMessage?: string | undefined): Promise<void>;
declare function doesNotThrow(this: void, callback: TypedFunction<void, any>, failureMessage?: string | undefined): void;
declare function doesNotThrow(this: void, callback: TypedFunction<void, MaybePromise<any>> | Promise<any>, failureMessage?: string | undefined): MaybePromise<void>;
declare function doesNotThrowCheck(this: void, callbackOrPromise: TypedFunction<void, never>): boolean;
declare function doesNotThrowCheck(this: void, callbackOrPromise: TypedFunction<void, Promise<any>> | Promise<any>): Promise<boolean>;
declare function doesNotThrowCheck(this: void, callback: TypedFunction<void, any>): boolean;
declare function doesNotThrowCheck(this: void, callback: TypedFunction<void, MaybePromise<any>> | Promise<any>): MaybePromise<boolean>;
declare function doesNotThrowAssertWrap(this: void, callback: TypedFunction<void, never>, failureMessage?: string | undefined): never;
declare function doesNotThrowAssertWrap<Value>(this: void, callbackOrPromise: TypedFunction<void, Promise<Value>> | Promise<Value>, failureMessage?: string | undefined): Promise<Value>;
declare function doesNotThrowAssertWrap<Value>(this: void, callback: TypedFunction<void, Value>, failureMessage?: string | undefined): Value;
declare function doesNotThrowCheckWrap(this: void, callback: TypedFunction<void, never>, failureMessage?: string | undefined): undefined;
declare function doesNotThrowCheckWrap<Value>(this: void, callbackOrPromise: TypedFunction<void, Promise<Value>> | Promise<Value>, failureMessage?: string | undefined): Promise<Value | undefined>;
declare function doesNotThrowCheckWrap<Value>(this: void, callback: TypedFunction<void, Value>, failureMessage?: string | undefined): Value | undefined;
export declare const throwGuards: {
    assert: {
        /**
         * If a function input is provided:
         *
         * Calls that function and asserts that the function throw an error, comparing the error with
         * the given {@link ErrorMatchOptions}, if provided.
         *
         * If a promise is provided:
         *
         * Awaits the promise and asserts that the promise rejected with an error, comparing the error
         * with the given {@link ErrorMatchOptions}, if provided.
         *
         * This assertion will automatically type itself as async vs async based on the input. (A
         * promise or async function inputs results in async. Otherwise, sync.)
         *
         * Performs no type guarding.
         *
         * @example
         *
         * ```ts
         * import {assert} from '@augment-vir/assert';
         *
         * assert.throws(() => {
         *     throw new Error();
         * }); // passes
         * assert.throws(
         *     () => {
         *         throw new Error();
         *     },
         *     {matchMessage: 'hi'},
         * ); // fails
         * await assert.throws(Promise.reject()); // passes
         * assert.throws(() => {}); // fails
         * ```
         *
         * @throws {@link AssertionError} If the assertion fails.
         */
        throws: typeof throws;
        /**
         * If a function input is provided:
         *
         * Calls that function and asserts that the function does not throw an error.
         *
         * If a promise is provided:
         *
         * Awaits the promise and asserts that the promise does not reject.
         *
         * This assertion will automatically type itself as async vs sync based on the input. (A promise
         * or async function inputs results in async. Otherwise, sync.)
         *
         * Performs no type guarding.
         *
         * @example
         *
         * ```ts
         * import {assert} from '@augment-vir/assert';
         *
         * assert.doesNotThrow(() => {}); // passes
         * assert.doesNotThrow(() => {
         *     throw new Error();
         * }); // fails
         * await assert.doesNotThrow(Promise.resolve()); // passes
         * await assert.doesNotThrow(Promise.reject()); // fails
         * ```
         *
         * @throws {@link AssertionError} If the assertion fails.
         */
        doesNotThrow: typeof doesNotThrow;
        /**
         * Asserts that a value is an instance of the built-in `Error` class and compares it to the
         * given {@link ErrorMatchOptions}, if provided.
         *
         * Type guards the input.
         *
         * @example
         *
         * ```ts
         * import {assert} from '@augment-vir/assert';
         *
         * assert.isError(new Error()); // passes
         * assert.isError(new Error(), {matchMessage: 'hi'}); // fails
         * assert.isError({message: 'not an error'}); // fails
         * ```
         *
         * @throws {@link AssertionError} If the assertion fails.
         */
        isError: typeof isError;
    };
    check: {
        /**
         * If a function input is provided:
         *
         * Calls that function and checks that the function throw an error, comparing the error with
         * the given {@link ErrorMatchOptions}, if provided.
         *
         * If a promise is provided:
         *
         * Awaits the promise and checks that the promise rejected with an error, comparing the
         * error with the given {@link ErrorMatchOptions}, if provided.
         *
         * This assertion will automatically type itself as async vs async based on the input. (A
         * promise or async function inputs results in async. Otherwise, sync.)
         *
         * Performs no type guarding.
         *
         * @example
         *
         * ```ts
         * import {check} from '@augment-vir/assert';
         *
         * check.throws(() => {
         *     throw new Error();
         * }); // returns `true`
         * check.throws(
         *     () => {
         *         throw new Error();
         *     },
         *     {matchMessage: 'hi'},
         * ); // returns `false`
         * await check.throws(Promise.reject()); // returns `true`
         * check.throws(() => {}); // returns `false`
         * ```
         */
        throws: typeof throwsCheck;
        /**
         * If a function input is provided:
         *
         * Calls that function and checks that the function does not throw an error.
         *
         * If a promise is provided:
         *
         * Awaits the promise and checks that the promise does not reject.
         *
         * This assertion will automatically type itself as async vs sync based on the input. (A
         * promise or async function inputs results in async. Otherwise, sync.)
         *
         * Performs no type guarding.
         *
         * @example
         *
         * ```ts
         * import {check} from '@augment-vir/assert';
         *
         * check.doesNotThrow(() => {}); // returns `true`
         * check.doesNotThrow(() => {
         *     throw new Error();
         * }); // returns `false`
         * await check.doesNotThrow(Promise.resolve()); // returns `true`
         * await check.doesNotThrow(Promise.reject()); // returns `false`
         * ```
         */
        doesNotThrow: typeof doesNotThrowCheck;
        /**
         * Checks that a value is an instance of the built-in `Error` class and compares it to the
         * given {@link ErrorMatchOptions}, if provided.
         *
         * Type guards the input.
         *
         * @example
         *
         * ```ts
         * import {check} from '@augment-vir/assert';
         *
         * check.isError(new Error()); // returns `true`
         * check.isError(new Error(), {matchMessage: 'hi'}); // returns `false`
         * check.isError({message: 'not an error'}); // returns `false`
         * ```
         */
        isError(this: void, actual: unknown, matchOptions?: ErrorMatchOptions | undefined): actual is Error;
    };
    assertWrap: {
        /**
         * If a function input is provided:
         *
         * Calls that function and asserts that the function throw an error, comparing the error
         * with the given {@link ErrorMatchOptions}, if provided. Returns the Error if the assertion
         * passes.
         *
         * If a promise is provided:
         *
         * Awaits the promise and asserts that the promise rejected with an error, comparing the
         * error with the given {@link ErrorMatchOptions}, if provided. Returns the Error if the
         * assertion passes.
         *
         * This assertion will automatically type itself as async vs async based on the input. (A
         * promise or async function inputs results in async. Otherwise, sync.)
         *
         * Performs no type guarding.
         *
         * @example
         *
         * ```ts
         * import {assertWrap} from '@augment-vir/assert';
         *
         * assertWrap.throws(() => {
         *     throw new Error();
         * }); // returns the thrown error
         * assertWrap.throws(
         *     () => {
         *         throw new Error();
         *     },
         *     {matchMessage: 'hi'},
         * ); // throws an error
         * await assertWrap.throws(Promise.reject()); // returns the rejection
         * assertWrap.throws(() => {}); // throws an error
         * ```
         *
         * @returns The Error if the assertion passes.
         * @throws {@link AssertionError} If the assertion fails.
         */
        throws: typeof throwsAssertWrap;
        /**
         * If a function input is provided:
         *
         * Calls that function and asserts that the function does not throw an error. Returns the
         * function's return value if the assertion passes.
         *
         * If a promise is provided:
         *
         * Awaits the promise and asserts that the promise does not reject. Returns the resolved
         * value if the assertion passes.
         *
         * This assertion will automatically type itself as async vs sync based on the input. (A
         * promise or async function inputs results in async. Otherwise, sync.)
         *
         * Performs no type guarding.
         *
         * @example
         *
         * ```ts
         * import {assertWrap} from '@augment-vir/assert';
         *
         * assertWrap.doesNotThrow(() => 'value'); // returns `'value'`
         * assertWrap.doesNotThrow(() => {
         *     throw new Error();
         * }); // throws an error
         * await assertWrap.doesNotThrow(Promise.resolve('value')); // returns `'value'`
         * await assertWrap.doesNotThrow(Promise.reject()); // throws an error
         * ```
         *
         * @returns The callback's return value (or the resolved value) if the assertion passes.
         * @throws {@link AssertionError} If the assertion fails.
         */
        doesNotThrow: typeof doesNotThrowAssertWrap;
        /**
         * Asserts that a value is an instance of the built-in `Error` class and compares it to the
         * given {@link ErrorMatchOptions}, if provided.
         *
         * Type guards the input.
         *
         * @example
         *
         * ```ts
         * import {assertWrap} from '@augment-vir/assert';
         *
         * assertWrap.isError(new Error()); // returns the error instance
         * assertWrap.isError(new Error(), {matchMessage: 'hi'}); // throws an error
         * assertWrap.isError({message: 'not an error'}); // throws an error
         * ```
         *
         * @returns The value if the assertion passes.
         * @throws {@link AssertionError} If the assertion fails.
         */
        isError<Actual>(this: void, actual: Actual, matchOptions?: ErrorMatchOptions | undefined, failureMessage?: string | undefined): NarrowToExpected<Actual, Error>;
    };
    checkWrap: {
        /**
         * If a function input is provided:
         *
         * Calls that function and checks that the function throw an error, comparing the error with
         * the given {@link ErrorMatchOptions}, if provided. Returns the error if the check passes,
         * otherwise `undefined`.
         *
         * If a promise is provided:
         *
         * Awaits the promise and checks that the promise rejected with an error, comparing the
         * error with the given {@link ErrorMatchOptions}, if provided. Returns the error if the
         * check passes, otherwise `undefined`.
         *
         * This assertion will automatically type itself as async vs async based on the input. (A
         * promise or async function inputs results in async. Otherwise, sync.)
         *
         * Performs no type guarding.
         *
         * @example
         *
         * ```ts
         * import {checkWrap} from '@augment-vir/assert';
         *
         * checkWrap.throws(() => {
         *     throw new Error();
         * }); // returns the thrown error
         * await checkWrap.throws(Promise.reject()); // returns the rejection
         * checkWrap.throws(() => {}); // returns `undefined`
         * ```
         *
         * @returns The Error if the check passes, otherwise `undefined`.
         */
        throws: typeof throwsCheckWrap;
        /**
         * If a function input is provided:
         *
         * Calls that function and checks that the function does not throw an error. Returns the
         * function's return value if the check passes, otherwise `undefined`.
         *
         * If a promise is provided:
         *
         * Awaits the promise and checks that the promise does not reject. Returns the resolved
         * value if the check passes, otherwise `undefined`.
         *
         * This assertion will automatically type itself as async vs sync based on the input. (A
         * promise or async function inputs results in async. Otherwise, sync.)
         *
         * Performs no type guarding.
         *
         * @example
         *
         * ```ts
         * import {checkWrap} from '@augment-vir/assert';
         *
         * checkWrap.doesNotThrow(() => 'value'); // returns `'value'`
         * checkWrap.doesNotThrow(() => {
         *     throw new Error();
         * }); // returns `undefined`
         * await checkWrap.doesNotThrow(Promise.resolve('value')); // returns `'value'`
         * await checkWrap.doesNotThrow(Promise.reject()); // returns `undefined`
         * ```
         *
         * @returns The callback's return value (or the resolved value) if the check passes,
         *   otherwise `undefined`.
         */
        doesNotThrow: typeof doesNotThrowCheckWrap;
        /**
         * Checks that a value is an instance of the built-in `Error` class and compares it to the
         * given {@link ErrorMatchOptions}, if provided. Returns the error if the check passes,
         * otherwise `undefined`.
         *
         * Type guards the input.
         *
         * @example
         *
         * ```ts
         * import {checkWrap} from '@augment-vir/assert';
         *
         * checkWrap.isError(new Error()); // returns the Error
         * checkWrap.isError(new Error(), {matchMessage: 'hi'}); // returns `undefined`
         * checkWrap.isError({message: 'not an error'}); // returns `undefined`
         * ```
         *
         * @returns The Error if the check passes, otherwise `undefined`.
         */
        isError<Actual>(this: void, actual: Actual, matchOptions?: ErrorMatchOptions | undefined): NarrowToExpected<Actual, Error> | undefined;
    };
    waitUntil: {
        /**
         * Repeatedly calls a callback until it throws an error, comparing the error with the given
         * {@link ErrorMatchOptions}, if provided (as the first input). Once the callback throws an
         * Error, that Error is returned. If the attempts time out, an error is thrown.
         *
         * This assertion will automatically type itself as async vs async based on the input. (A
         * promise or async function inputs results in async. Otherwise, sync.)
         *
         * Unlike the other `.throws` guards, `waitUntil.throws` does not allow a Promise input,
         * only a callback input.
         *
         * Performs no type guarding.
         *
         * @example
         *
         * ```ts
         * import {waitUntil} from '@augment-vir/assert';
         *
         * await waitUntil.throws(() => {
         *     throw new Error();
         * }); // returns the thrown error
         * await waitUntil.throws(Promise.reject()); // not allowed
         * await waitUntil.throws(() => {}); // throws an error
         * await waitUntil.throws({matchMessage: 'hi'}, () => {
         *     throw new Error('bye');
         * }); // throws an error
         * ```
         *
         * @returns The Error once it passes.
         * @throws {@link AssertionError} On timeout.
         */
        throws: typeof throwsWaitUntil;
        /**
         * Repeatedly calls a callback until it does not throw an error. Once the callback does not
         * throw, its return value is returned. If the attempts time out, an error is thrown.
         *
         * Unlike the other `.doesNotThrow` guards, `waitUntil.doesNotThrow` does not allow a
         * Promise input, only a callback input.
         *
         * Performs no type guarding.
         *
         * @example
         *
         * ```ts
         * import {waitUntil} from '@augment-vir/assert';
         *
         * await waitUntil.doesNotThrow(() => 'value'); // returns `'value'`
         * await waitUntil.doesNotThrow(() => {
         *     throw new Error();
         * }); // throws an error on timeout
         * ```
         *
         * @returns The callback's return value once it passes.
         * @throws {@link AssertionError} On timeout.
         */
        doesNotThrow: <Value>(this: void, callback: TypedFunction<void, MaybePromise<Value>>, options?: WaitUntilOptions | undefined, failureMessage?: string | undefined) => Promise<Value>;
        /**
         * Repeatedly calls a callback until is output is an instance of the built-in `Error` class
         * and compares it to the given {@link ErrorMatchOptions}, if provided. Once the callback
         * output passes, that Error is returned. If the attempts time out, an error is thrown.
         *
         * Type guards the input.
         *
         * @example
         *
         * ```ts
         * import {waitUntil} from '@augment-vir/assert';
         *
         * await waitUntil.isError(new Error()); // returns the error instance
         * await waitUntil.isError(new Error(), {matchMessage: 'hi'}); // throws an error
         * await waitUntil.isError({message: 'not an error'}); // throws an error
         * ```
         *
         * @returns The callback output once it passes.
         * @throws {@link AssertionError} On timeout.
         */
        isError: <Actual>(this: void, matchOptions: ErrorMatchOptions | undefined, callback: () => MaybePromise<Actual>, options?: WaitUntilOptions | undefined, failureMessage?: string | undefined) => Promise<NarrowToExpected<Actual, Error>>;
    };
};
export {};
