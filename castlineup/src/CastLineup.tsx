import React from "react";
import { AbsoluteFill, Img, staticFile } from "remotion";

/**
 * TrendDock — Cast Lineup Carousel Slide
 *
 * Design spec (confirmed with Samad, BuildWithSamad — Aug 2026):
 * - Canvas: 1080x1350 (4:5) — matches TMDB_W/TMDB_H already used in render.py
 * - Background: FIXED BuildWithSamad brand colors, same every day (not
 *   derived per-movie). Midnight Black base, Electric Orange accents.
 * - All 5 cast members present on every slide (no rotating membership).
 * - The member at `featuredIndex` is enlarged, full color, dragged forward.
 * - The other 4 sit in a back row of vertical strips, teal-tinted duotone
 *   (extends the 3-strip pattern from the Jungkook reference to 4 strips).
 * - Movie logo (TMDB PNG, or text-title fallback) sits at the top of
 *   every slide.
 * - Info block beneath the featured cutout: name, "AS {character}" tagline,
 *   and a short one-line Gemini-generated character description.
 *
 * Image inputs (`imageUrl` on each CastMember) are expected to be
 * ALREADY background-removed transparent PNGs (rembg output) by the time
 * they reach this component — CastLineup only composites, it does not
 * cut anything out itself.
 */

// ── Brand palette (BuildWithSamad — fixed, not derived per movie) ─────────
const BRAND_BLACK = "#0D0D0D";
const BRAND_ORANGE = "#FF6B2B";
const BRAND_WHITE = "#FFFFFF";
const BRAND_GREY = "#2A2A2A";
const TEAL_TINT = "#1B6E6E";

export const CANVAS_W = 1080;
export const CANVAS_H = 1350;

export type CastMember = {
  name: string;
  character: string;
  /** Gemini-generated one-liner. Only rendered for the featured member. */
  tagline?: string;
  /** Transparent PNG cutout (rembg output). Falls back to a placeholder if absent. */
  imageUrl?: string;
};

export type CastLineupProps = {
  members: CastMember[]; // exactly 5
  featuredIndex: number; // 0-4
  movieTitle: string;
  /** Transparent TMDB logo PNG. If absent, movieTitle is shown as styled text instead. */
  movieLogoUrl?: string;
};

// Silhouette placeholder used only when a member has no imageUrl yet
// (e.g. rembg step hasn't run / preview mode). Never meant to ship in
// a real post — a missing cutout should block the render, not post a
// silhouette, but this keeps the component testable in isolation.
const PlaceholderFigure: React.FC<{ label: string }> = ({ label }) => (
  <AbsoluteFill
    style={{
      justifyContent: "center",
      alignItems: "center",
      backgroundColor: BRAND_GREY,
    }}
  >
    <div
      style={{
        fontFamily: "Arial, sans-serif",
        fontWeight: 700,
        fontSize: 48,
        color: "rgba(255,255,255,0.35)",
      }}
    >
      {label}
    </div>
  </AbsoluteFill>
);

const BackStrip: React.FC<{ member: CastMember; widthPx: number }> = ({
  member,
  widthPx,
}) => (
  <div
    style={{
      position: "relative",
      width: widthPx,
      height: 950,
      overflow: "hidden",
      backgroundColor: BRAND_GREY,
    }}
  >
    {member.imageUrl ? (
      <Img
        src={member.imageUrl}
        style={{
          width: "100%",
          height: "100%",
          objectFit: "cover",
          // Duotone-teal treatment: desaturate then tint, matching the
          // Jungkook reference's back-row strip treatment.
          filter: "grayscale(1) contrast(1.05) brightness(0.75)",
        }}
      />
    ) : (
      <PlaceholderFigure label={member.name.split(" ")[0]} />
    )}
    {/* Teal tint overlay — mix-blend-mode gives the duotone color without
        needing a real image-processing pass, since this is a CSS-level
        treatment applied at render time, not baked into the source PNG. */}
    <div
      style={{
        position: "absolute",
        inset: 0,
        backgroundColor: TEAL_TINT,
        mixBlendMode: "color",
        opacity: 0.85,
      }}
    />
    <div
      style={{
        position: "absolute",
        inset: 0,
        background:
          "linear-gradient(180deg, rgba(13,13,13,0.15) 0%, rgba(13,13,13,0.55) 100%)",
      }}
    />
  </div>
);

export const CastLineup: React.FC<CastLineupProps> = ({
  members,
  featuredIndex,
  movieTitle,
  movieLogoUrl,
}) => {
  if (members.length !== 5) {
    throw new Error(
      `CastLineup requires exactly 5 members, got ${members.length}`
    );
  }

  const featured = members[featuredIndex];
  const backRow = members.filter((_, i) => i !== featuredIndex);
  const stripWidth = CANVAS_W / 4;

  return (
    <AbsoluteFill style={{ backgroundColor: BRAND_BLACK }}>
      {/* ── Back row: 4 teal-duotone strips ───────────────────────────── */}
      <AbsoluteFill style={{ top: 190, flexDirection: "row" }}>
        {backRow.map((member, i) => (
          <BackStrip key={i} member={member} widthPx={stripWidth} />
        ))}
      </AbsoluteFill>

      {/* ── Brand accent sweep behind featured cutout ─────────────────── */}
      <div
        style={{
          position: "absolute",
          left: 0,
          right: 0,
          top: 260,
          height: 8,
          backgroundColor: BRAND_ORANGE,
          opacity: 0.9,
        }}
      />

      {/* ── Featured cutout: full color, dragged forward, largest ─────── */}
      <div
        style={{
          position: "absolute",
          left: (CANVAS_W - 760) / 2,
          top: 300,
          width: 760,
          height: 980,
        }}
      >
        {featured.imageUrl ? (
          <Img
            src={featured.imageUrl}
            style={{
              width: "100%",
              height: "100%",
              objectFit: "cover",
              objectPosition: "center top",
              filter: "drop-shadow(0px 20px 40px rgba(0,0,0,0.6))",
            }}
          />
        ) : (
          <PlaceholderFigure label={featured.name} />
        )}
      </div>

      {/* ── Top: movie logo / text-title fallback ─────────────────────── */}
      <AbsoluteFill
        style={{
          top: 40,
          height: 130,
          justifyContent: "center",
          alignItems: "center",
        }}
      >
        {movieLogoUrl ? (
          <Img
            src={movieLogoUrl}
            style={{ maxWidth: 640, maxHeight: 110, objectFit: "contain" }}
          />
        ) : (
          <div
            style={{
              fontFamily: "Arial, sans-serif",
              fontWeight: 800,
              fontSize: 46,
              letterSpacing: 2,
              color: BRAND_WHITE,
              textTransform: "uppercase",
              textAlign: "center",
              textShadow: "0px 2px 10px rgba(0,0,0,0.8)",
            }}
          >
            {movieTitle}
          </div>
        )}
      </AbsoluteFill>

      {/* ── Bottom scrim so text stays legible over the cutout ─────────── */}
      <div
        style={{
          position: "absolute",
          left: 0,
          right: 0,
          bottom: 0,
          height: 420,
          background:
            "linear-gradient(180deg, rgba(13,13,13,0) 0%, rgba(13,13,13,0.92) 45%, rgba(13,13,13,1) 100%)",
        }}
      />

      {/* ── Info block: name / "AS character" / one-line description ──── */}
      <AbsoluteFill
        style={{
          top: "auto",
          bottom: 60,
          height: 300,
          justifyContent: "flex-end",
          alignItems: "center",
          padding: "0 80px",
        }}
      >
        <div
          style={{
            fontFamily: "Arial, sans-serif",
            fontWeight: 800,
            fontSize: 58,
            color: BRAND_WHITE,
            textAlign: "center",
            lineHeight: 1.05,
          }}
        >
          {featured.name}
        </div>
        <div
          style={{
            fontFamily: "Arial, sans-serif",
            fontWeight: 700,
            fontSize: 26,
            letterSpacing: 3,
            color: BRAND_ORANGE,
            textTransform: "uppercase",
            marginTop: 10,
          }}
        >
          AS {featured.character}
        </div>
        {featured.tagline ? (
          <div
            style={{
              fontFamily: "Arial, sans-serif",
              fontWeight: 400,
              fontSize: 26,
              color: "rgba(255,255,255,0.82)",
              textAlign: "center",
              marginTop: 16,
              maxWidth: 780,
              lineHeight: 1.35,
            }}
          >
            {featured.tagline}
          </div>
        ) : null}
      </AbsoluteFill>
    </AbsoluteFill>
  );
};
