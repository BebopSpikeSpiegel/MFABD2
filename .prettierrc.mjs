import * as multilineArrays from "prettier-plugin-multiline-arrays";
import * as maafwSort from "@nekosu/prettier-plugin-maafw-sort";

// 解析前删掉对象/数组里的空行,弥补 prettier「保留 1 个空行且不可关闭」的行为,
// 还原旧 schema 格式化器删光空行的效果。链式叠加:先删空行,再交给原 preprocess
// (multilineArrays 注入数组标记)。JSON 字符串里的换行是转义的 \n,不会被误伤。
function stripBlankLines(plugin) {
    for (const name of ["json", "jsonc"]) {
        const parser = plugin.parsers?.[name];
        if (!parser) continue;
        const prev = parser.preprocess;
        parser.preprocess = (text, options) => {
            const stripped = text.replace(/\n([ \t]*\n)+/g, "\n");
            return prev ? prev(stripped, options) : stripped;
        };
    }
    return plugin;
}

export default {
    plugins: [
        stripBlankLines(maafwSort.patchPlugin(multilineArrays)),
    ],
    multilineArraysWrapThreshold: 1,
    maafwInterfacePatterns: [
        "/interface\\.json",
    ],
    tabWidth: 4,
    printWidth: 120,
    useTabs: false,
    bracketSameLine: true,
    bracketSpacing: false,
    endOfLine: "auto",
    overrides: [
        {
            files: [
                "**/*.yml",
                "**/*.yaml",
            ],
            options: {
                parser: "yaml",
                tabWidth: 2,
            },
        },
        {
            files: [
                "*.json",
            ],
            options: {
                parser: "json",
                useTabs: false,
                bracketSameLine: false,
            },
        },
    ],
};
