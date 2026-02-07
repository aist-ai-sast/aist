module.exports = {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        sans: ["IBM Plex Sans", "ui-sans-serif", "system-ui"],
        mono: ["IBM Plex Mono", "ui-monospace", "SFMono-Regular"],
      },
      colors: {
        night: {
          900: "#0b0f1b",
          800: "#0d1321",
          700: "#141c2f",
          600: "#18223b",
          500: "#223055",
        },
        brand: {
          500: "#4dd4ff",
          600: "#2bb7e6",
        },
        amber: {
          400: "#f8c74d",
        },
        danger: {
          500: "#ff6b6b",
        },
      },
      boxShadow: {
        panel: "0 24px 60px rgba(8, 12, 20, 0.45)",
      },
    },
  },
  plugins: [],
};
