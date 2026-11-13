// Preloaded with `node --import` by the helper checks: web/src/lib files import each other without an extension (the
// bundler's way), so this hook adds ".ts" when node resolves a relative specifier that has none.
import { registerHooks } from "node:module";

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier.startsWith("./") && !/\.\w+$/.test(specifier)) {
      return nextResolve(specifier + ".ts", context);
    }
    return nextResolve(specifier, context);
  },
});
