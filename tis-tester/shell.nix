{ pkgs ? import <nixpkgs> {} }:
pkgs.mkShell {
  packages = [
    (pkgs.python3.withPackages (ps: [ ps.pyyaml ps.pytest ]))
    pkgs.bluez
    pkgs.iw
    pkgs.kmod
  ];
  shellHook = ''
    export PYTHONPATH=$PWD:$PYTHONPATH
    alias tis-test="python -m tis_tester.cli"
    echo "tis-tester dev shell — try: tis-test interactive --mock"
  '';
}
