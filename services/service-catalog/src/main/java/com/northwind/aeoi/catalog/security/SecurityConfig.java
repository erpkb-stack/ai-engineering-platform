package com.northwind.aeoi.catalog.security;

import java.io.IOException;
import java.io.InputStream;
import jakarta.servlet.DispatcherType;
import java.security.interfaces.RSAPublicKey;
import java.time.Duration;
import java.time.Instant;
import java.util.List;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.core.io.Resource;
import org.springframework.http.HttpMethod;
import org.springframework.security.config.Customizer;
import org.springframework.security.config.annotation.web.builders.HttpSecurity;
import org.springframework.security.config.http.SessionCreationPolicy;
import org.springframework.security.converter.RsaKeyConverters;
import org.springframework.security.oauth2.core.DelegatingOAuth2TokenValidator;
import org.springframework.security.oauth2.core.OAuth2Error;
import org.springframework.security.oauth2.core.OAuth2TokenValidator;
import org.springframework.security.oauth2.core.OAuth2TokenValidatorResult;
import org.springframework.security.oauth2.jose.jws.SignatureAlgorithm;
import org.springframework.security.oauth2.jwt.Jwt;
import org.springframework.security.oauth2.jwt.JwtClaimNames;
import org.springframework.security.oauth2.jwt.JwtDecoder;
import org.springframework.security.oauth2.jwt.JwtIssuerValidator;
import org.springframework.security.oauth2.jwt.JwtTimestampValidator;
import org.springframework.security.oauth2.jwt.NimbusJwtDecoder;
import org.springframework.security.web.SecurityFilterChain;

/**
 * Who may read (the same rules as libs/security Authenticator.verify):
 * RS256 only, our issuer and audience, exp + iat required, a SERVICE principal
 * (sub = service:name) with scope catalog:read. A delegated token (act / token_use=delegated) is
 * never a bearer (ADR-019). Spring maps "scope" to SCOPE_* authorities.
 */
@Configuration
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET) // not in import mode
public class SecurityConfig {

    public static final String SCOPE = "SCOPE_catalog:read";
    static final Duration LEEWAY = Duration.ofSeconds(30); // libs/security: 30 s, not Spring's 60

    @Bean
    SecurityFilterChain api(HttpSecurity http) throws Exception {
        http.authorizeHttpRequests(a -> a
                        // Spring's /error forward: a real 500 must stay a 500, not become a 403
                        .dispatcherTypeMatchers(DispatcherType.ERROR).permitAll()
                        .requestMatchers(HttpMethod.GET, "/actuator/health", "/actuator/health/**")
                        .permitAll()
                        .requestMatchers(HttpMethod.GET, "/v1/openapi.yaml").permitAll()
                        .requestMatchers(HttpMethod.GET, "/v1/services").hasAuthority(SCOPE)
                        .anyRequest().denyAll())
                .oauth2ResourceServer(o -> o.jwt(Customizer.withDefaults()))
                .csrf(c -> c.disable()) // a stateless bearer-token API: no cookies, no forms
                .sessionManagement(s -> s.sessionCreationPolicy(SessionCreationPolicy.STATELESS));
        return http.build();
    }

    @Bean
    JwtDecoder jwtDecoder(
            @Value("${catalog.jwt.public-key-file}") Resource publicKey,
            @Value("${catalog.jwt.issuer}") String issuer,
            @Value("${catalog.jwt.audience}") String audience) throws IOException {
        RSAPublicKey key;
        try (InputStream in = publicKey.getInputStream()) {
            key = RsaKeyConverters.x509().convert(in);
        }
        NimbusJwtDecoder decoder = NimbusJwtDecoder.withPublicKey(key)
                .signatureAlgorithm(SignatureAlgorithm.RS256)
                .build();
        decoder.setJwtValidator(new DelegatingOAuth2TokenValidator<>(
                new JwtTimestampValidator(LEEWAY), // exp/nbf, IF present (see serviceTokenOnly)
                new JwtIssuerValidator(issuer),
                audienceIs(audience),
                SecurityConfig::serviceTokenOnly));
        return decoder;
    }

    static OAuth2TokenValidator<Jwt> audienceIs(String audience) {
        return jwt -> {
            List<String> aud = jwt.getAudience();
            return aud != null && aud.contains(audience)
                    ? OAuth2TokenValidatorResult.success()
                    : failure("wrong audience");
        };
    }

    static OAuth2TokenValidatorResult serviceTokenOnly(Jwt jwt) {
        if (jwt.getExpiresAt() == null || jwt.getIssuedAt() == null) {
            return failure("exp and iat are required"); // the default validator lets a missing exp pass
        }
        if (jwt.getIssuedAt().isAfter(Instant.now().plus(LEEWAY))) {
            return failure("issued in the future");
        }
        if ("delegated".equals(jwt.getClaimAsString("token_use")) || jwt.hasClaim("act")) {
            return failure("a delegated token is never a bearer");
        }
        String sub = jwt.getClaimAsString(JwtClaimNames.SUB);
        if (sub == null || !sub.startsWith("service:") || sub.length() == "service:".length()) {
            return failure("only service tokens may read the catalog");
        }
        return OAuth2TokenValidatorResult.success();
    }

    private static OAuth2TokenValidatorResult failure(String why) {
        return OAuth2TokenValidatorResult.failure(new OAuth2Error("invalid_token", why, null));
    }
}
