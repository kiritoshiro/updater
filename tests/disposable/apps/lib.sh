# Helpers shared by setup.sh and accept.sh (sourced).

# testcommit <repo> <branch> <message> <shell snippet run in a clone>: prints the new branch head.
testcommit() {
  local bare=/srv/origin/$1.git work=/tmp/work-$1 owner
  owner=$(stat -c %U "$bare")
  rm -rf "$work"
  git clone -q -b "$2" "$bare" "$work"
  (cd "$work" && eval "$4" && git add -A && git commit -q -m "$3")
  git -C "$work" push -q origin "$2"
  chown -R "$owner:$owner" "$bare"
  git -C "$bare" rev-parse "refs/heads/$2"
}

# gate <sha> passed|failed|pending|missing
gate() {
  python3 -c 'import json, sys; p = "/srv/fakegh/gates.json"; d = json.load(open(p)); d[sys.argv[1]] = sys.argv[2]; json.dump(d, open(p, "w"))' "$1" "$2"
}

collect() {
  (cd /opt/site-watch && python3 -c 'import sitewatch_test_shim; from sitewatch.collector import main; main()' >/dev/null)
}
