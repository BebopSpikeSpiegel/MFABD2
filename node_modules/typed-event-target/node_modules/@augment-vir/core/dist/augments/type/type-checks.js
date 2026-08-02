/**
 * These type predicates are copied from `type-fest` v5.6 rather than imported from it. Owning them
 * here keeps `@augment-vir` (and its consumers) decoupled from `type-fest`'s evolving internals:
 * these utilities leak through the public `.d.ts` of downstream packages, so a `type-fest` upgrade
 * could otherwise silently break consumers. These local copies use only built-in types so they
 * never reference `type-fest`.
 */
export {};
