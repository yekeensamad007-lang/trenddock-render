import React from "react";
import { Composition } from "remotion";
import { CastLineup, CastMember, CANVAS_W, CANVAS_H } from "./CastLineup";

/**
 * Mock data for local preview/testing ONLY. In production, `members`,
 * `movieTitle`, and `movieLogoUrl` are populated from:
 *   - cast_fetcher.py (name, character, TMDB profile photo -> rembg cutout)
 *   - a new generate_character_tagline() Gemini call (one-line description)
 *   - TMDB /movie/{id}/images (logo PNG, text fallback if none exists)
 * No real photos are wired in yet — imageUrl is intentionally omitted so
 * the placeholder figures render, proving the LAYOUT works before any
 * image pipeline is connected.
 */
const MOCK_MEMBERS: CastMember[] = [
  { name: "Marcus Reid", character: "Detective Sarah Voss" },
  { name: "Elena Cho", character: "Dr. Alan Whitfield" },
  {
    name: "Tobias Grant",
    character: "Captain Reyes",
    tagline:
      "The hardened investigator whose one lead could unravel the whole conspiracy.",
  },
  { name: "Priya Nandan", character: "Agent Kolt" },
  { name: "David Okafor", character: "The Informant" },
];

export const Root: React.FC = () => {
  return (
    <>
      <Composition
        id="CastLineup"
        component={CastLineup}
        durationInFrames={1}
        fps={30}
        width={CANVAS_W}
        height={CANVAS_H}
        defaultProps={{
          members: MOCK_MEMBERS,
          featuredIndex: 2,
          movieTitle: "Skyline Horizon",
          movieLogoUrl: undefined,
        }}
      />
    </>
  );
};
