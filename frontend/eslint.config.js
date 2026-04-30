import js from "@eslint/js";
import tseslint from "typescript-eslint";
import reactPlugin from "eslint-plugin-react";
import globals from "globals";

/**
 * Flat ESLint config.
 *
 * The primary purpose of this configuration is to enforce safe rendering of
 * untrusted text (Requirements 13.1, 13.2): nicknames and typed content must
 * always flow through React's default text interpolation and never through
 * ``dangerouslySetInnerHTML``. The ``react/no-danger`` rule is set to "error"
 * so any future use of that prop fails the lint step and blocks the build.
 */
export default tseslint.config(
  {
    ignores: ["dist/**", "node_modules/**", "coverage/**"],
  },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ["**/*.{ts,tsx,js,jsx}"],
    plugins: {
      react: reactPlugin,
    },
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "module",
      parserOptions: {
        ecmaFeatures: { jsx: true },
      },
      globals: {
        ...globals.browser,
        ...globals.node,
      },
    },
    settings: {
      react: { version: "detect" },
    },
    rules: {
      // Safe rendering of untrusted text (Requirements 13.1, 13.2).
      // ``dangerouslySetInnerHTML`` bypasses React's built-in escaping and
      // would allow nickname / typed content to be interpreted as HTML or
      // script. Forbid it anywhere in the source tree.
      "react/no-danger": "error",
      // Honor the underscore-prefix convention for intentionally unused
      // parameters (e.g., fake fetch stubs that must match the fetch
      // signature). TypeScript's own ``noUnusedParameters`` already enforces
      // this for non-underscore names during the build.
      "@typescript-eslint/no-unused-vars": [
        "error",
        {
          argsIgnorePattern: "^_",
          varsIgnorePattern: "^_",
          caughtErrorsIgnorePattern: "^_",
        },
      ],
    },
  },
  {
    // Test files run under Vitest globals and commonly rely on ``any`` for
    // flexible fake fetch/response shapes; relax a few rules that are not
    // relevant to the safety goal of this config.
    files: ["**/*.test.{ts,tsx}", "vitest.setup.ts"],
    languageOptions: {
      globals: {
        ...globals.browser,
        ...globals.node,
        vi: "readonly",
        describe: "readonly",
        it: "readonly",
        test: "readonly",
        expect: "readonly",
        beforeEach: "readonly",
        afterEach: "readonly",
        beforeAll: "readonly",
        afterAll: "readonly",
      },
    },
    rules: {
      "@typescript-eslint/no-explicit-any": "off",
      "@typescript-eslint/no-unused-expressions": "off",
    },
  },
);
