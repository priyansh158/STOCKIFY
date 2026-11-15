import type { MetadataRoute } from "next";

/** The web manifest: the name and icons a browser or home screen shows. The colour is --color-paper (tokens.css) as sRGB. */
export default function manifest(): MetadataRoute.Manifest {
  return {
    name: "STOCKIFY",
    short_name: "STOCKIFY",
    description: "NSE simulation lab: record market data, backtest and paper-trade strategies.",
    start_url: "/",
    display: "standalone",
    background_color: "#0b110e",
    theme_color: "#0b110e",
    icons: [
      { src: "/brand/icon-192.png", sizes: "192x192", type: "image/png" },
      { src: "/brand/icon-512.png", sizes: "512x512", type: "image/png" },
    ],
  };
}
