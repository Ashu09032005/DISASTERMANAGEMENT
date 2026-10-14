package com.roadmanagement.controller;

import jakarta.servlet.http.HttpServletRequest;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpEntity;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpMethod;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.http.client.SimpleClientHttpRequestFactory;
import org.springframework.security.core.Authentication;
import org.springframework.security.oauth2.client.authentication.OAuth2AuthenticationToken;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.client.HttpStatusCodeException;
import org.springframework.web.client.ResourceAccessException;
import org.springframework.web.client.RestTemplate;

import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.util.Arrays;
import java.util.Locale;
import java.util.Set;
import java.util.stream.Collectors;

/**
 * Forwards /engine/** to the Python safety engine (modules M1-M6).
 *
 * The gateway keeps Google sign-in in one place and tells the engine who the
 * caller is through headers: X-User-Email, X-User-Role (ADMIN if the e-mail is
 * listed in app.admin-emails) and the shared secret X-Engine-Key.
 */
@RestController
public class EngineProxyController {

    @Value("${engine.url:http://localhost:8000}")
    private String engineUrl;

    @Value("${engine.key:}")
    private String engineKey;

    @Value("${app.admin-emails:}")
    private String adminEmails;

    @Value("${app.demo-admin:false}")
    private boolean demoAdmin;

    private final RestTemplate rest;

    public EngineProxyController() {
        SimpleClientHttpRequestFactory f = new SimpleClientHttpRequestFactory();
        f.setConnectTimeout(5_000);
        f.setReadTimeout(120_000); // scenario advance can take a while
        this.rest = new RestTemplate(f);
    }

    @RequestMapping("/engine/**")
    public ResponseEntity<byte[]> proxy(HttpServletRequest request,
                                        @RequestBody(required = false) byte[] body,
                                        Authentication authentication) {

        String path = request.getRequestURI().substring(request.getContextPath().length() + "/engine".length());
        if (path.isEmpty()) {
            path = "/";
        }
        if (path.contains("..") || path.contains("//")) {
            return error(HttpStatus.BAD_REQUEST, "invalid path");
        }
        String query = request.getQueryString();
        String base = engineUrl.startsWith("http") ? engineUrl : "http://" + engineUrl; // Render gives host:port
        URI target = URI.create(base + path + (query != null ? "?" + query : ""));

        String email = emailOf(authentication);
        HttpHeaders headers = new HttpHeaders();
        if (request.getContentType() != null) {
            headers.set(HttpHeaders.CONTENT_TYPE, request.getContentType());
        }
        headers.set("X-Engine-Key", engineKey);
        headers.set("X-User-Email", email);
        headers.set("X-User-Role", isAdmin(email) ? "ADMIN" : "USER");

        try {
            ResponseEntity<byte[]> r = rest.exchange(target, HttpMethod.valueOf(request.getMethod()),
                    new HttpEntity<>(body, headers), byte[].class);
            HttpHeaders out = new HttpHeaders();
            if (r.getHeaders().getContentType() != null) {
                out.setContentType(r.getHeaders().getContentType());
            }
            return new ResponseEntity<>(r.getBody(), out, r.getStatusCode());
        } catch (HttpStatusCodeException e) {
            return ResponseEntity.status(e.getStatusCode())
                    .contentType(MediaType.APPLICATION_JSON)
                    .body(e.getResponseBodyAsByteArray());
        } catch (ResourceAccessException e) {
            return error(HttpStatus.BAD_GATEWAY, "safety engine is not reachable at " + engineUrl);
        }
    }

    private boolean isAdmin(String email) {
        if (demoAdmin) {
            return true;
        }
        Set<String> admins = Arrays.stream(adminEmails.split(","))
                .map(s -> s.trim().toLowerCase(Locale.ROOT))
                .filter(s -> !s.isEmpty())
                .collect(Collectors.toSet());
        return admins.contains(email.toLowerCase(Locale.ROOT));
    }

    private static String emailOf(Authentication auth) {
        if (auth instanceof OAuth2AuthenticationToken token) {
            Object email = token.getPrincipal().getAttribute("email");
            if (email != null) {
                return email.toString();
            }
            return token.getName();
        }
        return "anonymous";
    }

    private static ResponseEntity<byte[]> error(HttpStatus status, String detail) {
        String json = "{\"detail\":\"" + detail.replace("\"", "'") + "\"}";
        return ResponseEntity.status(status).contentType(MediaType.APPLICATION_JSON)
                .body(json.getBytes(StandardCharsets.UTF_8));
    }
}
