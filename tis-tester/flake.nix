{
  description = "TIS test controller for the Quectel FCS960K-N (AIC8800D80 U02)";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-24.05";

  outputs = { self, nixpkgs }:
    let
      forAllSystems = f: nixpkgs.lib.genAttrs
        [ "x86_64-linux" "aarch64-linux" ]
        (system: f nixpkgs.legacyPackages.${system});
    in
    {
      packages = forAllSystems (pkgs: rec {
        tis-tester = pkgs.python3Packages.buildPythonApplication {
          pname = "tis-tester";
          version = "1.1.0";
          src = ./.;
          format = "pyproject";
          nativeBuildInputs = [ pkgs.python3Packages.setuptools ];
          propagatedBuildInputs = [ pkgs.python3Packages.pyyaml ];
          # Runtime host tools the backends shell out to:
          makeWrapperArgs = [
            "--prefix PATH : ${pkgs.lib.makeBinPath [ pkgs.bluez pkgs.iw pkgs.kmod ]}"
          ];
        };
        default = tis-tester;
      });

      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          packages = [
            (pkgs.python3.withPackages (ps: [ ps.pyyaml ps.pytest ]))
            pkgs.bluez          # hcitool / hciconfig for BT DTM
            pkgs.iw
            pkgs.kmod
          ];
          shellHook = ''
            export PYTHONPATH=$PWD:$PYTHONPATH
            alias tis-test="python -m tis_tester.cli"
            echo "tis-tester dev shell — try: tis-test interactive --mock"
          '';
        };
      });
    };
}
