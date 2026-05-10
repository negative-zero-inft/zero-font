{
  description = "zero typeface builder - standard and mono variants";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs =
    {
      self,
      nixpkgs,
      flake-utils,
    }:
    flake-utils.lib.eachDefaultSystem (
      system:
      let
        pkgs = nixpkgs.legacyPackages.${system};

        mkZeroFont =
          {
            name,
            isMono ? false,
          }:
          pkgs.python3Packages.buildPythonApplication {
            pname = "zero-font-${name}";
            version = "1.0.0";
            src = ./.;

            # this forces fonttools into the build environment properly
            propagatedBuildInputs = [ pkgs.python3Packages.fonttools ];

            # disable phases that don't make sense for a font
            dontUnpack = false;
            format = "other";

            buildPhase = ''
              runHook preBuild

              mkdir -p $out/share/fonts/opentype

              python3 builder.py \
                --src ./glyphs \
                --output $out/share/fonts/opentype/Zero-${if isMono then "Mono" else "Regular"}.otf \
                ${if isMono then "--mono" else ""}

              runHook postBuild
            '';

            installPhase = ''
              runHook preInstall
              # we already moved the font in buildPhase, so just make sure $out exists
              runHook postInstall
            '';
          };
      in
      {
        packages = {
          standard = mkZeroFont {
            name = "standard";
            isMono = false;
          };
          mono = mkZeroFont {
            name = "mono";
            isMono = true;
          };
          default = self.packages.${system}.standard;
        };

        devShells.default = pkgs.mkShell {
          buildInputs = [ (pkgs.python3.withPackages (ps: [ ps.fonttools ])) ];
        };
      }
    );
}
