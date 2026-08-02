/**
 * All standardized HTTP methods.
 *
 * @category HTTP
 * @category Package : @augment-vir/common
 * @package [`@augment-vir/common`](https://www.npmjs.com/package/@augment-vir/common)
 * @see https://developer.mozilla.org/docs/Web/HTTP/Methods
 */
export declare const HttpMethod: {
    readonly Get: "GET";
    readonly Head: "HEAD";
    readonly Options: "OPTIONS";
    readonly Trace: "TRACE";
    readonly Put: "PUT";
    readonly Delete: "DELETE";
    readonly Post: "POST";
    readonly Patch: "PATCH";
    readonly Connect: "CONNECT";
};
/**
 * All standardized HTTP methods.
 *
 * @category HTTP
 * @category Package : @augment-vir/common
 * @package [`@augment-vir/common`](https://www.npmjs.com/package/@augment-vir/common)
 * @see https://developer.mozilla.org/docs/Web/HTTP/Methods
 */
export type HttpMethod = (typeof HttpMethod)[keyof typeof HttpMethod];
