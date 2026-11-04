import { join } from "node:path";

/** Directory names inside the disposable smoke directory `tmp`. */
export function layout(tmp: string) {
  return {
    tmp,
    home: join(tmp, "home"),
    profile: join(tmp, "profile"),
    root: join(tmp, "root"),
    target: join(tmp, "target"),
    inputs: join(tmp, "inputs"),
    bin: join(tmp, "bin"),
    proxy: join(tmp, "proxy"),
    work: join(tmp, "work"),
    empty: join(tmp, "empty"),
  };
}
